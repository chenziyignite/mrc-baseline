import json
import logging
import os
from functools import partial
from multiprocessing import Pool, cpu_count

import numpy as np
from tqdm import tqdm

from transformers.file_utils import is_tf_available, is_torch_available

if is_torch_available():
    import torch
    from torch.utils.data import TensorDataset

if is_tf_available():
    import tensorflow as tf

logger = logging.getLogger(__name__)


def _improve_answer_span(doc_tokens, answer_tokens, input_start, input_end):
    """Returns tokenized answer spans that better match the annotated answer."""
    # tok_answer_text = " ".join(tokenizer.tokenize(orig_answer_text))
    #
    # for new_start in range(input_start, input_end + 1):
    #     for new_end in range(input_end, new_start - 1, -1):
    #         text_span = " ".join(doc_tokens[new_start: (new_end + 1)])
    #         if text_span == tok_answer_text:
    #             return (new_start, new_end)

    for new_start in range(0, len(doc_tokens)):
        for new_end in range(len(doc_tokens)-1, new_start - 1, -1):
            if ''.join(doc_tokens[new_start:(new_end + 1)]) == ''.join(answer_tokens):
                return (new_start, new_end)

    return (input_start, input_end)


def _check_is_max_context(doc_spans, cur_span_index, position):
    """Check if this is the 'max context' doc span for the token."""
    best_score = None
    best_span_index = None
    for (span_index, doc_span) in enumerate(doc_spans):
        end = doc_span.start + doc_span.length - 1
        if position < doc_span.start:
            continue
        if position > end:
            continue
        num_left_context = position - doc_span.start
        num_right_context = end - position
        score = min(num_left_context, num_right_context) + 0.01 * doc_span.length
        if best_score is None or score > best_score:
            best_score = score
            best_span_index = span_index

    return cur_span_index == best_span_index


def _new_check_is_max_context(doc_spans, cur_span_index, position):
    """Check if this is the 'max context' doc span for the token."""
    # if len(doc_spans) == 1:
    # return True
    best_score = None
    best_span_index = None
    for (span_index, doc_span) in enumerate(doc_spans):
        end = doc_span["start"] + doc_span["length"] - 1
        if position < doc_span["start"]:
            continue
        if position > end:
            continue
        num_left_context = position - doc_span["start"]
        num_right_context = end - position
        score = min(num_left_context, num_right_context) + 0.01 * doc_span["length"]
        if best_score is None or score > best_score:
            best_score = score
            best_span_index = span_index

    return cur_span_index == best_span_index


def _is_whitespace(c):
    if c == " " or c == "\t" or c == "\r" or c == "\n" or ord(c) == 0x202F:
        return True
    return False


def convert_example_to_features(example, max_seq_length, doc_stride, max_query_length, is_training):
    features = []

    doc_tokens = tokenizer.tokenize(example.context_text)

    # get start and end positions in paragraph token sequence
    if is_training and not example.is_impossible:
        answer_tokens = tokenizer.tokenize(example.answer_text)
        start_position = example.start_position  # original start position before tokenizing
        end_position = example.end_position  # original end position before tokenizing
        tok_start_position, tok_end_position = _improve_answer_span(doc_tokens, answer_tokens, start_position, end_position)

    spans = []
    # if len(example.context_text) > max_seq_length - len(example.question_text):
    #     print(example.qas_id)
    truncated_query = tokenizer.encode(
        example.question_text, add_special_tokens=False, truncation=True, max_length=max_query_length
    )
    # =2
    sequence_added_tokens = (
        tokenizer.max_len - tokenizer.max_len_single_sentence + 1
        if "roberta" in str(type(tokenizer)) or "camembert" in str(type(tokenizer))
        else tokenizer.max_len - tokenizer.max_len_single_sentence
    )
    # =3
    sequence_pair_added_tokens = tokenizer.max_len - tokenizer.max_len_sentences_pair

    span_doc_tokens = doc_tokens
    while len(spans) * doc_stride < len(doc_tokens):
        encoded_dict = tokenizer.encode_plus(
            truncated_query,
            span_doc_tokens,
            truncation='only_second',
            padding="max_length",
            max_length=max_seq_length,
            return_overflowing_tokens=True,
            stride=max_seq_length - doc_stride - len(truncated_query) - sequence_pair_added_tokens,
            return_token_type_ids=True,
            return_attention_mask=True
        )
        # len of paragraph tokens sequence of this span
        paragraph_len = min(
            len(doc_tokens) - len(spans) * doc_stride,  # 当最后一个span
            max_seq_length - len(truncated_query) - sequence_pair_added_tokens,
        )
        if tokenizer.pad_token_id in encoded_dict['input_ids']:
            non_padded_ids = encoded_dict["input_ids"][: encoded_dict["input_ids"].index(tokenizer.pad_token_id)]
        else:
            non_padded_ids = encoded_dict['input_ids']

        tokens = tokenizer.convert_ids_to_tokens(non_padded_ids)

        token_to_orig_map = {}
        for i in range(paragraph_len):
            index = len(truncated_query)+sequence_added_tokens+i
            token_to_orig_map[index] = len(spans) * doc_stride + i

        encoded_dict['paragraph_len'] = paragraph_len
        encoded_dict['tokens'] = tokens
        encoded_dict["token_to_orig_map"] = token_to_orig_map
        encoded_dict["truncated_query_with_special_tokens_length"] = len(truncated_query) + sequence_added_tokens
        encoded_dict['token_is_max_context'] = {}
        encoded_dict['start'] = len(spans) * doc_stride
        encoded_dict['length'] = paragraph_len

        spans.append(encoded_dict)

        if 'overflowing_tokens' not in encoded_dict or (
                'overflowing_tokens' in encoded_dict and len(encoded_dict['overflowing_tokens']) == 0):
            break
        span_doc_tokens = encoded_dict['overflowing_tokens']

    for doc_span_index in range(len(spans)):
        for j in range(spans[doc_span_index]['paragraph_len']):
            is_max_context = _new_check_is_max_context(spans, doc_span_index, doc_span_index * doc_stride + j)
            index = (
                j
                if tokenizer.padding_side == "left"
                else spans[doc_span_index]["truncated_query_with_special_tokens_length"] + j
            )
            spans[doc_span_index]["token_is_max_context"][index] = is_max_context
    for span in spans:
        # Identify the position of the CLS token (normally =0)
        cls_index = span["input_ids"].index(tokenizer.cls_token_id)

        # p_mask: mask with 1 for token than cannot be in the answer (0 for token which can be in an answer)
        p_mask = np.ones_like(span["token_type_ids"])
        p_mask[len(truncated_query) + sequence_added_tokens:] = 0
        pad_token_indices = np.where(np.array(span["input_ids"]) == tokenizer.pad_token_id)
        special_token_indices = np.asarray(
            tokenizer.get_special_tokens_mask(span["input_ids"], already_has_special_tokens=True)
        ).nonzero()

        p_mask[pad_token_indices] = 1
        p_mask[special_token_indices] = 1

        # Set the cls index to 0: the CLS index can be used for impossible answers
        p_mask[cls_index] = 0

        span_is_impossible = example.is_impossible
        start_position = 0
        end_position = 0

        if is_training and not span_is_impossible:
            # For training, if our document chunk does not contain an annotation
            # we throw it out, since there is nothing to predict.
            doc_start = span['start']
            doc_end = span['start'] + span['length'] - 1
            out_of_span = False

            if not (tok_start_position >= doc_start and tok_end_position <= doc_end):
                out_of_span = True

            if out_of_span:
                start_position = cls_index
                end_position = cls_index
                span_is_impossible = True
            else:
                doc_offset = len(truncated_query) + sequence_added_tokens
                start_position = tok_start_position - doc_start + doc_offset
                end_position = tok_end_position - doc_start + doc_offset

        features.append(AlibabaFeatures(
            span['input_ids'],
            span['attention_mask'],
            span['token_type_ids'],
            cls_index,
            p_mask.tolist(),
            example_index=0,
            # Can not set unique_id and example_index here. They will be set after multiple processing.
            unique_id=0,
            paragraph_len=span['paragraph_len'],
            token_is_max_context=span['token_is_max_context'],
            tokens=span['tokens'],
            token_to_orig_map=span["token_to_orig_map"],
            start_position=start_position,
            end_position=end_position,
            is_impossible=span_is_impossible,
            qas_id=example.qas_id
        ))
    return features


def convert_example_to_features_init(tokenizer_for_convert):
    global tokenizer
    tokenizer = tokenizer_for_convert


def convert_examples_to_features(
        examples,
        tokenizer,
        max_seq_length,
        doc_stride,
        max_query_length,
        is_training,
        return_dataset=False,
        threads=1,
        tqdm_enabled=True,
):
    """
    Converts a list of examples into a list of features that can be directly given as input to a model.
    It is model-dependant and takes advantage of many of the tokenizer's features to create the model's inputs.

    Args:
        examples: list of :class:`~transformers.data.processors.squad.SquadExample`
        tokenizer: an instance of a child of :class:`~transformers.PreTrainedTokenizer`
        max_seq_length: The maximum sequence length of the inputs.
        doc_stride: The stride used when the context is too large and is split across several features.
        max_query_length: The maximum length of the query.
        is_training: whether to create features for model evaluation or model training.
        return_dataset: Default False. Either 'pt' or 'tf'.
            if 'pt': returns a torch.data.TensorDataset,
            if 'tf': returns a tf.data.Dataset
        threads: multiple processing threadsa-smi
        tqdm_enabled:

    Returns:
        list of :class:`~transformers.data.processors.squad.SquadFeatures`

    Example::

        processor = SquadV2Processor()
        examples = processor.get_dev_examples(data_dir)

        features = squad_convert_examples_to_features(
            examples=examples,
            tokenizer=tokenizer,
            max_seq_length=args.max_seq_length,
            doc_stride=args.doc_stride,
            max_query_length=args.max_query_length,
            is_training=not evaluate,
        )
    """

    # Defining helper methods
    features = []
    threads = min(threads, cpu_count())
    with Pool(threads, initializer=convert_example_to_features_init, initargs=(tokenizer,)) as p:
        annotate_ = partial(
            convert_example_to_features,
            max_seq_length=max_seq_length,
            doc_stride=doc_stride,
            max_query_length=max_query_length,
            is_training=is_training,
        )
        features = list(
            tqdm(
                p.imap(annotate_, examples, chunksize=32),
                total=len(examples),
                desc="convert squad examples to features",
                disable=not tqdm_enabled,
            )
        )
    new_features = []
    unique_id = 1000000000
    example_index = 0
    for example_features in tqdm(
        features, total=len(features), desc="add example index and unique id", disable=not tqdm_enabled
    ):
        if not example_features:
            continue
        for example_feature in example_features:
            example_feature.example_index = example_index
            example_feature.unique_id = unique_id
            new_features.append(example_feature)
            unique_id += 1
        example_index += 1
    features = new_features
    del new_features
    if return_dataset == "pt":
        if not is_torch_available():
            raise RuntimeError("PyTorch must be installed to return a PyTorch dataset.")

        # Convert to Tensors and build dataset
        all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long) # (total_num, max_seq_len)
        all_attention_masks = torch.tensor([f.attention_mask for f in features], dtype=torch.long)  # (total_num, max_seq_len)
        all_token_type_ids = torch.tensor([f.token_type_ids for f in features], dtype=torch.long)   # (total_num, max_seq_len)
        all_cls_index = torch.tensor([f.cls_index for f in features], dtype=torch.long) # (total_num, )
        all_p_mask = torch.tensor([f.p_mask for f in features], dtype=torch.float)   # (total_num, max_seq_len)
        all_is_impossible = torch.tensor([f.is_impossible for f in features], dtype=torch.float)    # (total_ num)

        if not is_training:
            all_feature_index = torch.arange(all_input_ids.size(0), dtype=torch.long)
            dataset = TensorDataset(all_input_ids, all_attention_masks, all_token_type_ids, all_feature_index,all_cls_index, all_p_mask)
        else:
            all_start_positions = torch.tensor([f.start_position for f in features], dtype=torch.long)  # (total_num)
            all_end_positions = torch.tensor([f.end_position for f in features], dtype=torch.long)  # (total_num)
            dataset = TensorDataset(
                all_input_ids,
                all_attention_masks,
                all_token_type_ids,
                all_start_positions,
                all_end_positions,
                all_cls_index,
                all_p_mask,
                all_is_impossible,
            )

        return features, dataset
    else:
        return features


class AlibabaProcessor:
    """f
    Processor for the Alibaba data set.
    """

    def _get_example_from_tensor_dict(self, tensor_dict, evaluate=False):
        if not evaluate:
            answer = tensor_dict["answers"]["text"][0].numpy().decode("utf-8")
            answer_start = tensor_dict["answers"]["answer_start"][0].numpy()
            answers = []
        else:
            answers = [
                {"answer_start": start.numpy(), "text": text.numpy().decode("utf-8")}
                for start, text in zip(tensor_dict["answers"]["answer_start"], tensor_dict["answers"]["text"])
            ]

            answer = None
            answer_start = None

        return AlibabaExample(
            qas_id=tensor_dict["id"].numpy().decode("utf-8"),
            question_text=tensor_dict["question"].numpy().decode("utf-8"),
            context_text=tensor_dict["context"].numpy().decode("utf-8"),
            answer_text=answer,
            start_position_character=answer_start,
            title=tensor_dict["title"].numpy().decode("utf-8"),
            answers=answers,
        )

    def get_examples_from_dataset(self, dataset, evaluate=False):
        """
        Creates a list of :class:`~transformers.data.processors.squad.SquadExample` using a TFDS dataset.

        Args:
            dataset: The tfds dataset loaded from `tensorflow_datasets.load("squad")`
            evaluate: boolean specifying if in evaluation mode or in training mode

        Returns:
            List of SquadExample

        Examples::
        """
        """
            >>> import tensorflow_datasets as tfds
            >>> dataset = tfds.load("squad")

            >>> training_examples = get_examples_from_dataset(dataset, evaluate=False)
            >>> evaluation_examples = get_examples_from_dataset(dataset, evaluate=True)

        """

        if evaluate:
            dataset = dataset["validation"]
        else:
            dataset = dataset["train"]

        examples = []
        for tensor_dict in tqdm(dataset):
            examples.append(self._get_example_from_tensor_dict(tensor_dict, evaluate=evaluate))

        return examples

    def get_train_examples(self, data_dir, filename):
        """
        Returns the training examples from the data directory.

        Args:
            data_dir: Directory containing the data files used for training and evaluating.
            filename: None by default, specify this if the training file has a different name than the original one
                which is `train-v1.1.json` and `train-v2.0.json` for squad versions 1.1 and 2.0 respectively.

        """
        if data_dir is None:
            data_dir = ""

        # if self.train_file is None:
        #     raise ValueError("SquadProcessor should be instantiated via SquadV1Processor or SquadV2Processor")

        with open(
                os.path.join(data_dir, filename), "r", encoding="utf-8"
        ) as reader:
            input_data = json.load(reader)["data"]
        return self._create_examples(input_data, 'train')

    def get_dev_examples(self, data_dir, filename):
        """
        Returns the evaluation example from the data directory.

        Args:
            data_dir: Directory containing the data files used for training and evaluating.
            filename: None by default, specify this if the evaluation file has a different name than the original one
                which is `train-v1.1.json` and `train-v2.0.json` for squad versions 1.1 and 2.0 respectively.
        """
        if data_dir is None:
            data_dir = ""

        # if self.dev_file is None:
        #     raise ValueError("SquadProcessor should be instantiated via SquadV1Processor or SquadV2Processor")

        with open(
                os.path.join(data_dir, filename), "r", encoding="utf-8"
        ) as reader:
            input_data = json.load(reader)["data"]
        return self._create_examples(input_data, 'dev')

    def _create_examples(self, input_data, set_type):
        is_training = set_type == "train"
        examples = []
        invalid_examples = []
        max_context_len = max_question_len = max_answer_len = 0
        for entry in tqdm(input_data):
            title = entry["title"]
            for paragraph in entry["paragraphs"]:
                context_text = paragraph["context"]  # str
                for qa in paragraph["qas"]:
                    qas_id = qa["id"]  # int
                    question_text = qa["question"]  # str
                    is_challenge = qa['is_challenge']  # bool
                    is_impossible = qa["is_impossible"]  # bool
                    start_position_character = None
                    answer_text = None
                    answers = []

                    if not is_impossible:
                        if is_training:     # SQuAD数据集的训练集每个样本只有一条回答
                            answer = qa["answers"][0]   # dict {text: , answer_start: }
                            answer_text = answer["text"]    # str
                            start_position_character = answer["answer_start"]   # int
                        else:   # SQuAD数据集验证集每个样本有多条回答（但阿里数据集只有一条）
                            answers = qa["answers"]     # list[dict]

                    example = AlibabaExample(
                        qas_id=qas_id,
                        question_text=question_text,
                        context_text=context_text,
                        answer_text=answer_text,
                        start_position_character=start_position_character,
                        title=title,
                        is_impossible=is_impossible,
                        is_challenge=is_challenge,
                        answers=answers,
                    )

                    if not question_text:
                        logger.warning('skip example:{}, question is null'.format(qas_id))
                        invalid_examples.append(example.qas_id)
                    elif is_training and (not example.is_impossible) and (example.context_text[example.start_position:(
                            example.end_position + 1)] != example.answer_text):
                        logger.warning("skip example:%d, Could not find answer: '%s' in '%s'", example.qas_id,
                                       example.answer_text,
                                       example.context_text[example.start_position:(example.end_position + 1)])
                        invalid_examples.append(example.qas_id)
                    else:
                        examples.append(example)
                        max_context_len = max(max_context_len, len(example.context_text))
                        max_question_len = max(max_question_len, len(example.question_text))
                        if not is_impossible:
                            if is_training:
                                max_answer_len = max(max_answer_len, len(example.answer_text))
                            else:
                                max_answer_len = max(max_answer_len, max([len(answer['text']) for answer in example.answers]))
        logger.info(
            'max_question_len:{}, max_context_len:{}, max_answer_len:{}'.format(max_question_len, max_context_len,
                                                                                max_answer_len))
        logger.info('invalid examples number: {}'.format(len(invalid_examples)))
        return examples


# class AlibabaV1Processor(AlibabaProcessor):
#     train_file = "train-v1.1.json"
#     dev_file = "dev-v1.1.json"


# class AlibabaV2Processor(AlibabaProcessor):
#     train_file = "train-v2.0.json"
#     dev_file = "dev-v2.0.json"


class AlibabaExample:
    """
    A single training/test example for the Squad dataset, as loaded from disk.

    Args:
        qas_id: The example's unique identifier
        question_text: The question string
        context_text: The context string
        answer_text: The answer string
        start_position_character: The character position of the start of the answer
        title: The title of the example
        is_challenge: False by default, set to True if the example is challenging.
        is_impossible: False by default, set to True if the example has no possible answer.
    """

    def __init__(
            self,
            qas_id,
            question_text,
            context_text,
            answer_text,
            start_position_character,
            title,
            answers=[],
            is_impossible=False,
            is_challenge=False,
    ):
        self.qas_id = qas_id
        self.question_text = question_text
        self.context_text = context_text
        self.answer_text = answer_text
        self.title = title
        self.is_impossible = is_impossible
        self.is_challenge = is_challenge
        self.answers = answers

        if not self.is_impossible and self.answer_text:     # answer_text has a value only during training stage
            self.start_position = start_position_character
            self.end_position = start_position_character + len(self.answer_text) - 1
        else:
            self.start_position = self.end_position = 0


class AlibabaFeatures(object):
    """
    Single squad example features to be fed to a model.
    Those features are model-specific and can be crafted from :class:`~transformers.data.processors.squad.SquadExample`
    using the :method:`~transformers.data.processors.squad.squad_convert_examples_to_features` method.

    Args:
        input_ids: Indices of input sequence tokens in the vocabulary.
        attention_mask: Mask to avoid performing attention on padding token indices.
        token_type_ids: Segment token indices to indicate first and second portions of the inputs.
        cls_index: the index of the CLS token.
        p_mask: Mask identifying tokens that can be answers vs. tokens that cannot.
            Mask with 1 for tokens than cannot be in the answer and 0 for token that can be in an answer
        example_index: the index of the example
        unique_id: The unique Feature identifier
        paragraph_len: The length of the context
        token_is_max_context: List of booleans identifying which tokens have their maximum context in this feature object.
            If a token does not have their maximum context in this feature object, it means that another feature object
            has more information related to that token and should be prioritized over this feature for that token.
        tokens: list of tokens corresponding to the input ids
        token_to_orig_map: mapping between the tokens and the original text, needed in order to identify the answer.
        start_position: start of the answer token index
        end_position: end of the answer token index
    """

    def __init__(
            self,
            input_ids,
            attention_mask,
            token_type_ids,
            cls_index,
            p_mask,
            example_index,
            unique_id,
            paragraph_len,
            token_is_max_context,
            tokens,
            token_to_orig_map,
            start_position,
            end_position,
            is_impossible,
            qas_id,
    ):
        self.input_ids = input_ids
        self.attention_mask = attention_mask
        self.token_type_ids = token_type_ids
        self.cls_index = cls_index
        self.p_mask = p_mask

        self.example_index = example_index
        self.unique_id = unique_id
        self.paragraph_len = paragraph_len
        self.token_is_max_context = token_is_max_context
        self.tokens = tokens
        self.token_to_orig_map = token_to_orig_map

        self.start_position = start_position
        self.end_position = end_position
        self.is_impossible = is_impossible
        self.qas_id = qas_id


class AlibabaResult(object):
    """
    Constructs a SquadResult which can be used to evaluate a model's output on the SQuAD dataset.

    Args:
        qas_id: The unique identifier corresponding to that example.
        start_logits: The logits corresponding to the start of the answer
        end_logits: The logits corresponding to the end of the answer
    """

    def __init__(self, unique_id, start_logits, end_logits, start_top_index=None, end_top_index=None, cls_logits=None):
        self.start_logits = start_logits
        self.end_logits = end_logits
        self.unique_id = unique_id

        if start_top_index:
            self.start_top_index = start_top_index
            self.end_top_index = end_top_index
            self.cls_logits = cls_logits
