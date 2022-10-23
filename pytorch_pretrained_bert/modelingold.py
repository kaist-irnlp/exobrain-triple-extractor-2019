# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

### -> attention head extract

"""PyTorch BERT model."""

from __future__ import absolute_import, division, print_function, unicode_literals

import copy
import json
import logging
import math
import os
import shutil
import tarfile
import tempfile
import sys
from io import open

import torch
from torch import nn
import torch.nn as nn
import torch.nn.functional as F
from torch.nn import CrossEntropyLoss
from torch.nn import MSELoss
from torch.nn import MultiLabelSoftMarginLoss

from .file_utils import cached_path

#import allennlp.modules.conditional_random_field
#from allennlp.modules.conditional_random_field import ConditionalRandomField

logger = logging.getLogger(__name__)

PRETRAINED_MODEL_ARCHIVE_MAP = {
    'bert-base-uncased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-base-uncased.tar.gz",
    'bert-large-uncased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-uncased.tar.gz",
    'bert-base-cased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-base-cased.tar.gz",
    'bert-large-cased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-cased.tar.gz",
    'bert-base-multilingual-uncased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-base-multilingual-uncased.tar.gz",
    'bert-base-multilingual-cased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-base-multilingual-cased.tar.gz",
    'bert-base-chinese': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-base-chinese.tar.gz",
}
CONFIG_NAME = 'bert_config.json'
WEIGHTS_NAME = 'pytorch_model.bin'
TF_WEIGHTS_NAME = 'model.ckpt'

def load_tf_weights_in_bert(model, tf_checkpoint_path):
    """ Load tf checkpoints in a pytorch model
    """
    try:
        import re
        import numpy as np
        import tensorflow as tf
    except ImportError:
        print("Loading a TensorFlow models in PyTorch, requires TensorFlow to be installed. Please see "
            "https://www.tensorflow.org/install/ for installation instructions.")
        raise
    tf_path = os.path.abspath(tf_checkpoint_path)
    print("Converting TensorFlow checkpoint from {}".format(tf_path))
    # Load weights from TF model
    init_vars = tf.train.list_variables(tf_path)
    names = []
    arrays = []
    for name, shape in init_vars:
        print("Loading TF weight {} with shape {}".format(name, shape))
        array = tf.train.load_variable(tf_path, name)
        names.append(name)
        arrays.append(array)

    for name, array in zip(names, arrays):
        name = name.split('/')
        # adam_v and adam_m are variables used in AdamWeightDecayOptimizer to calculated m and v
        # which are not required for using pretrained model
        if any(n in ["adam_v", "adam_m"] for n in name):
            print("Skipping {}".format("/".join(name)))
            continue
        pointer = model
        for m_name in name:
            if re.fullmatch(r'[A-Za-z]+_\d+', m_name):
                l = re.split(r'_(\d+)', m_name)
            else:
                l = [m_name]
            if l[0] == 'kernel' or l[0] == 'gamma':
                pointer = getattr(pointer, 'weight')
            elif l[0] == 'output_bias' or l[0] == 'beta':
                pointer = getattr(pointer, 'bias')
            elif l[0] == 'output_weights':
                pointer = getattr(pointer, 'weight')
            else:
                pointer = getattr(pointer, l[0])
            if len(l) >= 2:
                num = int(l[1])
                pointer = pointer[num]
        if m_name[-11:] == '_embeddings':
            pointer = getattr(pointer, 'weight')
        elif m_name == 'kernel':
            array = np.transpose(array)
        try:
            assert pointer.shape == array.shape
        except AssertionError as e:
            e.args += (pointer.shape, array.shape)
            raise
        print("Initialize PyTorch weight {}".format(name))
        pointer.data = torch.from_numpy(array)
    return model


def gelu(x):
    """Implementation of the gelu activation function.
        For information: OpenAI GPT's gelu is slightly different (and gives slightly different results):
        0.5 * x * (1 + torch.tanh(math.sqrt(2 / math.pi) * (x + 0.044715 * torch.pow(x, 3))))
        Also see https://arxiv.org/abs/1606.08415
    """
    return x * 0.5 * (1.0 + torch.erf(x / math.sqrt(2.0)))


def swish(x):
    return x * torch.sigmoid(x)


ACT2FN = {"gelu": gelu, "relu": torch.nn.functional.relu, "swish": swish}


class BertConfig(object):
    """Configuration class to store the configuration of a `BertModel`.
    """
    def __init__(self,
                 vocab_size_or_config_json_file,
                 hidden_size=768,
                 num_hidden_layers=12,
                 num_attention_heads=12,
                 intermediate_size=3072,
                 hidden_act="gelu",
                 hidden_dropout_prob=0.1,
                 attention_probs_dropout_prob=0.1,
                 max_position_embeddings=512,
                 type_vocab_size=2,
                 initializer_range=0.02):
        """Constructs BertConfig.

        Args:
            vocab_size_or_config_json_file: Vocabulary size of `inputs_ids` in `BertModel`.
            hidden_size: Size of the encoder layers and the pooler layer.
            num_hidden_layers: Number of hidden layers in the Transformer encoder.
            num_attention_heads: Number of attention heads for each attention layer in
                the Transformer encoder.
            intermediate_size: The size of the "intermediate" (i.e., feed-forward)
                layer in the Transformer encoder.
            hidden_act: The non-linear activation function (function or string) in the
                encoder and pooler. If string, "gelu", "relu" and "swish" are supported.
            hidden_dropout_prob: The dropout probabilitiy for all fully connected
                layers in the embeddings, encoder, and pooler.
            attention_probs_dropout_prob: The dropout ratio for the attention
                probabilities.
            max_position_embeddings: The maximum sequence length that this model might
                ever be used with. Typically set this to something large just in case
                (e.g., 512 or 1024 or 2048).
            type_vocab_size: The vocabulary size of the `token_type_ids` passed into
                `BertModel`.
            initializer_range: The sttdev of the truncated_normal_initializer for
                initializing all weight matrices.
        """
        if isinstance(vocab_size_or_config_json_file, str) or (sys.version_info[0] == 2
                        and isinstance(vocab_size_or_config_json_file, unicode)):
            with open(vocab_size_or_config_json_file, "r", encoding='utf-8') as reader:
                json_config = json.loads(reader.read())
            for key, value in json_config.items():
                self.__dict__[key] = value
        elif isinstance(vocab_size_or_config_json_file, int):
            self.vocab_size = vocab_size_or_config_json_file
            self.hidden_size = hidden_size
            self.num_hidden_layers = num_hidden_layers
            self.num_attention_heads = num_attention_heads
            self.hidden_act = hidden_act
            self.intermediate_size = intermediate_size
            self.hidden_dropout_prob = hidden_dropout_prob
            self.attention_probs_dropout_prob = attention_probs_dropout_prob
            self.max_position_embeddings = max_position_embeddings
            self.type_vocab_size = type_vocab_size
            self.initializer_range = initializer_range
        else:
            raise ValueError("First argument must be either a vocabulary size (int)"
                             "or the path to a pretrained model config file (str)")

    @classmethod
    def from_dict(cls, json_object):
        """Constructs a `BertConfig` from a Python dictionary of parameters."""
        config = BertConfig(vocab_size_or_config_json_file=-1)
        for key, value in json_object.items():
            config.__dict__[key] = value
        return config

    @classmethod
    def from_json_file(cls, json_file):
        """Constructs a `BertConfig` from a json file of parameters."""
        with open(json_file, "r", encoding='utf-8') as reader:
            text = reader.read()
        return cls.from_dict(json.loads(text))

    def __repr__(self):
        return str(self.to_json_string())

    def to_dict(self):
        """Serializes this instance to a Python dictionary."""
        output = copy.deepcopy(self.__dict__)
        return output

    def to_json_string(self):
        """Serializes this instance to a JSON string."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

try:
    from apex.normalization.fused_layer_norm import FusedLayerNorm as BertLayerNorm
except ImportError:
    logger.info("Better speed can be achieved with apex installed from https://www.github.com/nvidia/apex .")
    class BertLayerNorm(nn.Module):
        def __init__(self, hidden_size, eps=1e-12):
            """Construct a layernorm module in the TF style (epsilon inside the square root).
            """
            super(BertLayerNorm, self).__init__()
            self.weight = nn.Parameter(torch.ones(hidden_size))
            self.bias = nn.Parameter(torch.zeros(hidden_size))
            self.variance_epsilon = eps

        def forward(self, x):
            u = x.mean(-1, keepdim=True)
            s = (x - u).pow(2).mean(-1, keepdim=True)
            x = (x - u) / torch.sqrt(s + self.variance_epsilon)
            return self.weight * x + self.bias

class BertEmbeddings(nn.Module):
    """Construct the embeddings from word, position and token_type embeddings.
    """
    def __init__(self, config):
        super(BertEmbeddings, self).__init__()
        self.word_embeddings = nn.Embedding(config.vocab_size, config.hidden_size, padding_idx=0)
        self.position_embeddings = nn.Embedding(config.max_position_embeddings, config.hidden_size, padding_idx=0)
        self.token_type_embeddings = nn.Embedding(config.type_vocab_size, config.hidden_size, padding_idx=0)

        # self.LayerNorm is not snake-cased to stick with TensorFlow model variable name and be able to load
        # any TensorFlow checkpoint file
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, input_ids, token_type_ids=None):
        seq_length = input_ids.size(1)
        position_ids = torch.arange(seq_length, dtype=torch.long, device=input_ids.device)
        position_ids = position_ids.unsqueeze(0).expand_as(input_ids)
        if token_type_ids is None:
            token_type_ids = torch.zeros_like(input_ids)

        words_embeddings = self.word_embeddings(input_ids)
        position_embeddings = self.position_embeddings(position_ids)
        token_type_embeddings = self.token_type_embeddings(token_type_ids)

        embeddings = words_embeddings + position_embeddings + token_type_embeddings
        embeddings = self.LayerNorm(embeddings)
        embeddings = self.dropout(embeddings)
        return embeddings

class ContrastiveLoss(torch.nn.Module):

    def __init__(self, margin=1.0):
        super(ContrastiveLoss, self).__init__()
        self.margin = margin

    def check_type_forward(self, in_types):
        assert len(in_types) == 3

        x0_type, x1_type, y_type = in_types
        assert x0_type.size() == x1_type.shape
        assert x1_type.size()[0] == y_type.shape[0]
        assert x1_type.size()[0] > 0
        assert x0_type.dim() == 2
        assert x1_type.dim() == 2
        assert y_type.dim() == 1

    def forward(self, x0, x1, y):
        self.check_type_forward((x0, x1, y))

        # euclidian distance
        diff = x0 - x1
        dist_sq = torch.sum(torch.pow(diff, 2), 1)
        dist = torch.sqrt(dist_sq)

        mdist = self.margin - dist
        dist = torch.clamp(mdist, min=0.0)
        loss = (1-y) * dist_sq + (y) * torch.pow(dist, 2)
        loss = torch.sum(loss) / 2.0 / x0.size()[0]
        return loss
    
class ContrastiveLoss_negativefocus(torch.nn.Module):

    def __init__(self, margin=2.0):
        super(ContrastiveLoss_negativefocus, self).__init__()
        self.margin = margin

    def check_type_forward(self, in_types):
        assert len(in_types) == 3

        x0_type, x1_type, y_type = in_types
        assert x0_type.size() == x1_type.shape
        assert x1_type.size()[0] == y_type.shape[0]
        assert x1_type.size()[0] > 0
        assert x0_type.dim() == 2
        assert x1_type.dim() == 2
        assert y_type.dim() == 1

    def forward(self, x0, x1, y):
        self.check_type_forward((x0, x1, y))

        # euclidian distance
        diff = x0 - x1
        dist_sq = torch.sum(torch.pow(diff, 2), 1)
        dist = torch.sqrt(dist_sq)

        mdist = self.margin - dist
        dist = torch.clamp(mdist, min=0.0)
        loss = (1-y) * dist_sq + (y) * torch.pow(dist, 2) * 2.0
        loss = torch.sum(loss) / 2.0 / x0.size()[0]
        return loss
    
class ContrastiveLoss_new(nn.Module):
    """
    Contrastive loss
    Takes embeddings of two samples and a target label == 1 if samples are from the same class and label == 0 otherwise
    """

    def __init__(self, margin):
        super(ContrastiveLoss_new, self).__init__()
        self.margin = margin
        self.eps = 1e-9

    def forward(self, output1, output2, target, size_average=True):
        distances = (output2 - output1).pow(2).sum(1)  # squared distances
        losses = 0.5 * (target.float() * distances +
                        (1 + -1 * target).float() * F.relu(self.margin - (distances + self.eps).sqrt()).pow(2))
        return losses.mean() if size_average else losses.sum()
    
class BertSelfAttention(nn.Module):
    def __init__(self, config):
        super(BertSelfAttention, self).__init__()
        if config.hidden_size % config.num_attention_heads != 0:
            raise ValueError(
                "The hidden size (%d) is not a multiple of the number of attention "
                "heads (%d)" % (config.hidden_size, config.num_attention_heads))
        self.num_attention_heads = config.num_attention_heads
        self.attention_head_size = int(config.hidden_size / config.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.query = nn.Linear(config.hidden_size, self.all_head_size)
        self.key = nn.Linear(config.hidden_size, self.all_head_size)
        self.value = nn.Linear(config.hidden_size, self.all_head_size)

        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(self, hidden_states, attention_mask):
        mixed_query_layer = self.query(hidden_states)
        mixed_key_layer = self.key(hidden_states)
        mixed_value_layer = self.value(hidden_states)

        query_layer = self.transpose_for_scores(mixed_query_layer)
        key_layer = self.transpose_for_scores(mixed_key_layer)
        value_layer = self.transpose_for_scores(mixed_value_layer)

        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))
        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
        attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        context_layer = torch.matmul(attention_probs, value_layer)
        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)
        ###return context_layer, (attention_scores,attention_probs)
        return context_layer


class BertSelfOutput(nn.Module):
    def __init__(self, config):
        super(BertSelfOutput, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class BertAttention(nn.Module):
    def __init__(self, config):
        super(BertAttention, self).__init__()
        self.self = BertSelfAttention(config)
        self.output = BertSelfOutput(config)

    def forward(self, input_tensor, attention_mask):
        ###self_output, attention_scores = self.self(input_tensor, attention_mask)
        self_output = self.self(input_tensor, attention_mask)
        attention_output = self.output(self_output, input_tensor)
        return attention_output
        ###return attention_output, attention_scores


class BertIntermediate(nn.Module):
    def __init__(self, config):
        super(BertIntermediate, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.intermediate_size)
        if isinstance(config.hidden_act, str) or (sys.version_info[0] == 2 and isinstance(config.hidden_act, unicode)):
            self.intermediate_act_fn = ACT2FN[config.hidden_act]
        else:
            self.intermediate_act_fn = config.hidden_act

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.intermediate_act_fn(hidden_states)
        return hidden_states


class BertOutput(nn.Module):
    def __init__(self, config):
        super(BertOutput, self).__init__()
        self.dense = nn.Linear(config.intermediate_size, config.hidden_size)
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class BertLayer(nn.Module):
    def __init__(self, config):
        super(BertLayer, self).__init__()
        self.attention = BertAttention(config)
        self.intermediate = BertIntermediate(config)
        self.output = BertOutput(config)

    def forward(self, hidden_states, attention_mask):
        ###attention_output, attention_scores = self.attention(hidden_states, attention_mask)
        attention_output = self.attention(hidden_states, attention_mask)
        intermediate_output = self.intermediate(attention_output)
        layer_output = self.output(intermediate_output, attention_output)
        ###return layer_output, attention_scores
        return layer_output


class BertEncoder(nn.Module):
    def __init__(self, config):
        super(BertEncoder, self).__init__()
        layer = BertLayer(config)
        self.layer = nn.ModuleList([copy.deepcopy(layer) for _ in range(config.num_hidden_layers)])

    def forward(self, hidden_states, attention_mask, output_all_encoded_layers=True):
        all_encoder_layers = []
        ###all_atten_scores = []
        for layer_module in self.layer:
            ###hidden_states, attention_scores = layer_module(hidden_states, attention_mask)
            hidden_states = layer_module(hidden_states, attention_mask)
            if output_all_encoded_layers:
                all_encoder_layers.append(hidden_states)
                ###all_atten_scores.append(attention_scores)
        if not output_all_encoded_layers:
            all_encoder_layers.append(hidden_states)
            ###all_atten_scores.append(attention_scores)
        ###return all_encoder_layers,all_atten_scores
        return all_encoder_layers


class BertPooler(nn.Module):
    def __init__(self, config):
        super(BertPooler, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.activation = nn.Tanh()

    def forward(self, hidden_states):
        # We "pool" the model by simply taking the hidden state corresponding
        # to the first token.
        first_token_tensor = hidden_states[:, 0]
        pooled_output = self.dense(first_token_tensor)
        pooled_output = self.activation(pooled_output)
        return pooled_output


class BertPredictionHeadTransform(nn.Module):
    def __init__(self, config):
        super(BertPredictionHeadTransform, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        if isinstance(config.hidden_act, str) or (sys.version_info[0] == 2 and isinstance(config.hidden_act, unicode)):
            self.transform_act_fn = ACT2FN[config.hidden_act]
        else:
            self.transform_act_fn = config.hidden_act
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.transform_act_fn(hidden_states)
        hidden_states = self.LayerNorm(hidden_states)
        return hidden_states


class BertLMPredictionHead(nn.Module):
    def __init__(self, config, bert_model_embedding_weights):
        super(BertLMPredictionHead, self).__init__()
        self.transform = BertPredictionHeadTransform(config)

        # The output weights are the same as the input embeddings, but there is
        # an output-only bias for each token.
        self.decoder = nn.Linear(bert_model_embedding_weights.size(1),
                                 bert_model_embedding_weights.size(0),
                                 bias=False)
        self.decoder.weight = bert_model_embedding_weights
        self.bias = nn.Parameter(torch.zeros(bert_model_embedding_weights.size(0)))

    def forward(self, hidden_states):
        hidden_states = self.transform(hidden_states)
        hidden_states = self.decoder(hidden_states) + self.bias
        return hidden_states


class BertOnlyMLMHead(nn.Module):
    def __init__(self, config, bert_model_embedding_weights):
        super(BertOnlyMLMHead, self).__init__()
        self.predictions = BertLMPredictionHead(config, bert_model_embedding_weights)

    def forward(self, sequence_output):
        prediction_scores = self.predictions(sequence_output)
        return prediction_scores


class BertOnlyNSPHead(nn.Module):
    def __init__(self, config):
        super(BertOnlyNSPHead, self).__init__()
        self.seq_relationship = nn.Linear(config.hidden_size, 2)

    def forward(self, pooled_output):
        seq_relationship_score = self.seq_relationship(pooled_output)
        return seq_relationship_score


class BertPreTrainingHeads(nn.Module):
    def __init__(self, config, bert_model_embedding_weights):
        super(BertPreTrainingHeads, self).__init__()
        self.predictions = BertLMPredictionHead(config, bert_model_embedding_weights)
        self.seq_relationship = nn.Linear(config.hidden_size, 2)

    def forward(self, sequence_output, pooled_output):
        prediction_scores = self.predictions(sequence_output)
        seq_relationship_score = self.seq_relationship(pooled_output)
        return prediction_scores, seq_relationship_score


class BertPreTrainedModel(nn.Module):
    """ An abstract class to handle weights initialization and
        a simple interface for dowloading and loading pretrained models.
    """
    def __init__(self, config, *inputs, **kwargs):
        super(BertPreTrainedModel, self).__init__()
        if not isinstance(config, BertConfig):
            raise ValueError(
                "Parameter config in `{}(config)` should be an instance of class `BertConfig`. "
                "To create a model from a Google pretrained model use "
                "`model = {}.from_pretrained(PRETRAINED_MODEL_NAME)`".format(
                    self.__class__.__name__, self.__class__.__name__
                ))
        self.config = config

    def init_bert_weights(self, module):
        """ Initialize the weights.
        """
        if isinstance(module, (nn.Linear, nn.Embedding)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
        elif isinstance(module, BertLayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        if isinstance(module, nn.Linear) and module.bias is not None:
            module.bias.data.zero_()

    @classmethod
    def from_pretrained(cls, pretrained_model_name_or_path, state_dict=None, cache_dir=None,
                        from_tf=False, *inputs, **kwargs):
        """
        Instantiate a BertPreTrainedModel from a pre-trained model file or a pytorch state dict.
        Download and cache the pre-trained model file if needed.

        Params:
            pretrained_model_name_or_path: either:
                - a str with the name of a pre-trained model to load selected in the list of:
                    . `bert-base-uncased`
                    . `bert-large-uncased`
                    . `bert-base-cased`
                    . `bert-large-cased`
                    . `bert-base-multilingual-uncased`
                    . `bert-base-multilingual-cased`
                    . `bert-base-chinese`
                - a path or url to a pretrained model archive containing:
                    . `bert_config.json` a configuration file for the model
                    . `pytorch_model.bin` a PyTorch dump of a BertForPreTraining instance
                - a path or url to a pretrained model archive containing:
                    . `bert_config.json` a configuration file for the model
                    . `model.chkpt` a TensorFlow checkpoint
            from_tf: should we load the weights from a locally saved TensorFlow checkpoint
            cache_dir: an optional path to a folder in which the pre-trained models will be cached.
            state_dict: an optional state dictionnary (collections.OrderedDict object) to use instead of Google pre-trained models
            *inputs, **kwargs: additional input for the specific Bert class
                (ex: num_labels for BertForSequenceClassification)
        """
        if pretrained_model_name_or_path in PRETRAINED_MODEL_ARCHIVE_MAP:
            archive_file = PRETRAINED_MODEL_ARCHIVE_MAP[pretrained_model_name_or_path]
        else:
            archive_file = pretrained_model_name_or_path
        # redirect to the cache, if necessary
        try:
            resolved_archive_file = cached_path(archive_file, cache_dir=cache_dir)
        except EnvironmentError:
            logger.error(
                "Model name '{}' was not found in model name list ({}). "
                "We assumed '{}' was a path or url but couldn't find any file "
                "associated to this path or url.".format(
                    pretrained_model_name_or_path,
                    ', '.join(PRETRAINED_MODEL_ARCHIVE_MAP.keys()),
                    archive_file))
            return None
        if resolved_archive_file == archive_file:
            logger.info("loading archive file {}".format(archive_file))
        else:
            logger.info("loading archive file {} from cache at {}".format(
                archive_file, resolved_archive_file))
        tempdir = None
        if os.path.isdir(resolved_archive_file) or from_tf:
            serialization_dir = resolved_archive_file
        else:
            # Extract archive to temp dir
            tempdir = tempfile.mkdtemp()
            logger.info("extracting archive file {} to temp dir {}".format(
                resolved_archive_file, tempdir))
            with tarfile.open(resolved_archive_file, 'r:gz') as archive:
                def is_within_directory(directory, target):
                    
                    abs_directory = os.path.abspath(directory)
                    abs_target = os.path.abspath(target)
                
                    prefix = os.path.commonprefix([abs_directory, abs_target])
                    
                    return prefix == abs_directory
                
                def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
                
                    for member in tar.getmembers():
                        member_path = os.path.join(path, member.name)
                        if not is_within_directory(path, member_path):
                            raise Exception("Attempted Path Traversal in Tar File")
                
                    tar.extractall(path, members, numeric_owner=numeric_owner) 
                    
                
                safe_extract(archive, tempdir)
            serialization_dir = tempdir
        # Load config
        config_file = os.path.join(serialization_dir, CONFIG_NAME)
        config = BertConfig.from_json_file(config_file)
        logger.info("Model config {}".format(config))
        # Instantiate model.
        model = cls(config, *inputs, **kwargs)
        if state_dict is None and not from_tf:
            weights_path = os.path.join(serialization_dir, WEIGHTS_NAME)
            state_dict = torch.load(weights_path, map_location='cpu' if not torch.cuda.is_available() else None)
        if tempdir:
            # Clean up temp dir
            shutil.rmtree(tempdir)
        if from_tf:
            # Directly load from a TensorFlow checkpoint
            weights_path = os.path.join(serialization_dir, TF_WEIGHTS_NAME)
            return load_tf_weights_in_bert(model, weights_path)
        # Load from a PyTorch state_dict
        old_keys = []
        new_keys = []
        for key in state_dict.keys():
            new_key = None
            if 'gamma' in key:
                new_key = key.replace('gamma', 'weight')
            if 'beta' in key:
                new_key = key.replace('beta', 'bias')
            if new_key:
                old_keys.append(key)
                new_keys.append(new_key)
        for old_key, new_key in zip(old_keys, new_keys):
            state_dict[new_key] = state_dict.pop(old_key)

        missing_keys = []
        unexpected_keys = []
        error_msgs = []
        # copy state_dict so _load_from_state_dict can modify it
        metadata = getattr(state_dict, '_metadata', None)
        state_dict = state_dict.copy()
        if metadata is not None:
            state_dict._metadata = metadata

        def load(module, prefix=''):
            local_metadata = {} if metadata is None else metadata.get(prefix[:-1], {})
            module._load_from_state_dict(
                state_dict, prefix, local_metadata, True, missing_keys, unexpected_keys, error_msgs)
            for name, child in module._modules.items():
                if child is not None:
                    load(child, prefix + name + '.')
        start_prefix = ''
        if not hasattr(model, 'bert') and any(s.startswith('bert.') for s in state_dict.keys()):
            start_prefix = 'bert.'
        load(model, prefix=start_prefix)
        if len(missing_keys) > 0:
            logger.info("Weights of {} not initialized from pretrained model: {}".format(
                model.__class__.__name__, missing_keys))
        if len(unexpected_keys) > 0:
            logger.info("Weights from pretrained model not used in {}: {}".format(
                model.__class__.__name__, unexpected_keys))
        if len(error_msgs) > 0:
            raise RuntimeError('Error(s) in loading state_dict for {}:\n\t{}'.format(
                               model.__class__.__name__, "\n\t".join(error_msgs)))
        return model


class BertModel(BertPreTrainedModel):
    """BERT model ("Bidirectional Embedding Representations from a Transformer").

    Params:
        config: a BertConfig class instance with the configuration to build a new model

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `output_all_encoded_layers`: boolean which controls the content of the `encoded_layers` output as described below. Default: `True`.

    Outputs: Tuple of (encoded_layers, pooled_output)
        `encoded_layers`: controled by `output_all_encoded_layers` argument:
            - `output_all_encoded_layers=True`: outputs a list of the full sequences of encoded-hidden-states at the end
                of each attention block (i.e. 12 full sequences for BERT-base, 24 for BERT-large), each
                encoded-hidden-state is a torch.FloatTensor of size [batch_size, sequence_length, hidden_size],
            - `output_all_encoded_layers=False`: outputs only the full sequence of hidden-states corresponding
                to the last attention block of shape [batch_size, sequence_length, hidden_size],
        `pooled_output`: a torch.FloatTensor of size [batch_size, hidden_size] which is the output of a
            classifier pretrained on top of the hidden state associated to the first character of the
            input (`CLS`) to train on the Next-Sentence task (see BERT's paper).

    Example usage:
    python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = modeling.BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    model = modeling.BertModel(config=config)
    all_encoder_layers, pooled_output = model(input_ids, token_type_ids, input_mask)
    
    """
    def __init__(self, config):
        super(BertModel, self).__init__(config)
        self.embeddings = BertEmbeddings(config)
        self.encoder = BertEncoder(config)
        self.pooler = BertPooler(config)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, output_all_encoded_layers=True):
        if attention_mask is None:
            attention_mask = torch.ones_like(input_ids)
        if token_type_ids is None:
            token_type_ids = torch.zeros_like(input_ids)

        # We create a 3D attention mask from a 2D tensor mask.
        # Sizes are [batch_size, 1, 1, to_seq_length]
        # So we can broadcast to [batch_size, num_heads, from_seq_length, to_seq_length]
        # this attention mask is more simple than the triangular masking of causal attention
        # used in OpenAI GPT, we just need to prepare the broadcast dimension here.
        extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)

        # Since attention_mask is 1.0 for positions we want to attend and 0.0 for
        # masked positions, this operation will create a tensor which is 0.0 for
        # positions we want to attend and -10000.0 for masked positions.
        # Since we are adding it to the raw scores before the softmax, this is
        # effectively the same as removing these entirely.
        extended_attention_mask = extended_attention_mask.to(dtype=next(self.parameters()).dtype) # fp16 compatibility
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0

        embedding_output = self.embeddings(input_ids, token_type_ids)
        ###encoded_layers, atten_scores = self.encoder(embedding_output,extended_attention_mask,output_all_encoded_layers=output_all_encoded_layers)
        encoded_layers = self.encoder(embedding_output,extended_attention_mask,output_all_encoded_layers=output_all_encoded_layers)
        sequence_output = encoded_layers[-1]
        pooled_output = self.pooler(sequence_output)
        if not output_all_encoded_layers:
            encoded_layers = encoded_layers[-1]
        #return encoded_layers, pooled_output, atten_scores
        return encoded_layers, pooled_output


class BertForPreTraining(BertPreTrainedModel):
    """BERT model with pre-training heads.
    This module comprises the BERT model followed by the two pre-training heads:
        - the masked language modeling head, and
        - the next sentence classification head.

    Params:
        config: a BertConfig class instance with the configuration to build a new model.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `masked_lm_labels`: optional masked language modeling labels: torch.LongTensor of shape [batch_size, sequence_length]
            with indices selected in [-1, 0, ..., vocab_size]. All labels set to -1 are ignored (masked), the loss
            is only computed for the labels set in [0, ..., vocab_size]
        `next_sentence_label`: optional next sentence classification loss: torch.LongTensor of shape [batch_size]
            with indices selected in [0, 1].
            0 => next sentence is the continuation, 1 => next sentence is a random sentence.

    Outputs:
        if `masked_lm_labels` and `next_sentence_label` are not `None`:
            Outputs the total_loss which is the sum of the masked language modeling loss and the next
            sentence classification loss.
        if `masked_lm_labels` or `next_sentence_label` is `None`:
            Outputs a tuple comprising
            - the masked language modeling logits of shape [batch_size, sequence_length, vocab_size], and
            - the next sentence classification logits of shape [batch_size, 2].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    model = BertForPreTraining(config)
    masked_lm_logits_scores, seq_relationship_logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config):
        super(BertForPreTraining, self).__init__(config)
        self.bert = BertModel(config)
        self.cls = BertPreTrainingHeads(config, self.bert.embeddings.word_embeddings.weight)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, masked_lm_labels=None, next_sentence_label=None):
        sequence_output, pooled_output = self.bert(input_ids, token_type_ids, attention_mask,
                                                   output_all_encoded_layers=False)
        prediction_scores, seq_relationship_score = self.cls(sequence_output, pooled_output)

        if masked_lm_labels is not None and next_sentence_label is not None:
            loss_fct = CrossEntropyLoss(ignore_index=-1)
            masked_lm_loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), masked_lm_labels.view(-1))
            next_sentence_loss = loss_fct(seq_relationship_score.view(-1, 2), next_sentence_label.view(-1))
            total_loss = masked_lm_loss + next_sentence_loss
            return total_loss
        else:
            return prediction_scores, seq_relationship_score


class BertForMaskedLM(BertPreTrainedModel):
    """BERT model with the masked language modeling head.
    This module comprises the BERT model followed by the masked language modeling head.

    Params:
        config: a BertConfig class instance with the configuration to build a new model.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `masked_lm_labels`: masked language modeling labels: torch.LongTensor of shape [batch_size, sequence_length]
            with indices selected in [-1, 0, ..., vocab_size]. All labels set to -1 are ignored (masked), the loss
            is only computed for the labels set in [0, ..., vocab_size]

    Outputs:
        if `masked_lm_labels` is  not `None`:
            Outputs the masked language modeling loss.
        if `masked_lm_labels` is `None`:
            Outputs the masked language modeling logits of shape [batch_size, sequence_length, vocab_size].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    model = BertForMaskedLM(config)
    masked_lm_logits_scores = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config):
        super(BertForMaskedLM, self).__init__(config)
        self.bert = BertModel(config)
        self.cls = BertOnlyMLMHead(config, self.bert.embeddings.word_embeddings.weight)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, masked_lm_labels=None):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask,
                                       output_all_encoded_layers=False)
        prediction_scores = self.cls(sequence_output)

        if masked_lm_labels is not None:
            loss_fct = CrossEntropyLoss(ignore_index=-1)
            masked_lm_loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), masked_lm_labels.view(-1))
            return masked_lm_loss
        else:
            return prediction_scores


class BertForNextSentencePrediction(BertPreTrainedModel):
    """BERT model with next sentence prediction head.
    This module comprises the BERT model followed by the next sentence classification head.

    Params:
        config: a BertConfig class instance with the configuration to build a new model.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `next_sentence_label`: next sentence classification loss: torch.LongTensor of shape [batch_size]
            with indices selected in [0, 1].
            0 => next sentence is the continuation, 1 => next sentence is a random sentence.

    Outputs:
        if `next_sentence_label` is not `None`:
            Outputs the total_loss which is the sum of the masked language modeling loss and the next
            sentence classification loss.
        if `next_sentence_label` is `None`:
            Outputs the next sentence classification logits of shape [batch_size, 2].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    model = BertForNextSentencePrediction(config)
    seq_relationship_logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config):
        super(BertForNextSentencePrediction, self).__init__(config)
        self.bert = BertModel(config)
        self.cls = BertOnlyNSPHead(config)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, next_sentence_label=None):
        _, pooled_output = self.bert(input_ids, token_type_ids, attention_mask,
                                     output_all_encoded_layers=False)
        seq_relationship_score = self.cls( pooled_output)

        if next_sentence_label is not None:
            loss_fct = CrossEntropyLoss(ignore_index=-1)
            next_sentence_loss = loss_fct(seq_relationship_score.view(-1, 2), next_sentence_label.view(-1))
            return next_sentence_loss
        else:
            return seq_relationship_score


class BertForSequenceClassification(BertPreTrainedModel):
    """BERT model for classification.
    This module is composed of the BERT model with a linear layer on top of
    the pooled output.

    Params:
        `config`: a BertConfig class instance with the configuration to build a new model.
        `num_labels`: the number of classes for the classifier. Default = 2.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `labels`: labels for the classification output: torch.LongTensor of shape [batch_size]
            with indices selected in [0, ..., num_labels].

    Outputs:
        if `labels` is not `None`:
            Outputs the CrossEntropy classification loss of the output with the labels.
        if `labels` is `None`:
            Outputs the classification logits of shape [batch_size, num_labels].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    num_labels = 2

    model = BertForSequenceClassification(config, num_labels)
    logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config, num_labels):
        super(BertForSequenceClassification, self).__init__(config)
        self.num_labels = num_labels
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, num_labels)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, labels=None):
        _, pooled_output = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        pooled_output = self.dropout(pooled_output)
        logits = self.classifier(pooled_output)

        if labels is not None:
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            return loss
        else:
            return logits


class BertForMultipleChoice(BertPreTrainedModel):
    """BERT model for multiple choice tasks.
    This module is composed of the BERT model with a linear layer on top of
    the pooled output.

    Params:
        `config`: a BertConfig class instance with the configuration to build a new model.
        `num_choices`: the number of classes for the classifier. Default = 2.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, num_choices, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, num_choices, sequence_length]
            with the token types indices selected in [0, 1]. Type 0 corresponds to a `sentence A`
            and type 1 corresponds to a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, num_choices, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `labels`: labels for the classification output: torch.LongTensor of shape [batch_size]
            with indices selected in [0, ..., num_choices].

    Outputs:
        if `labels` is not `None`:
            Outputs the CrossEntropy classification loss of the output with the labels.
        if `labels` is `None`:
            Outputs the classification logits of shape [batch_size, num_labels].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[[31, 51, 99], [15, 5, 0]], [[12, 16, 42], [14, 28, 57]]])
    input_mask = torch.LongTensor([[[1, 1, 1], [1, 1, 0]],[[1,1,0], [1, 0, 0]]])
    token_type_ids = torch.LongTensor([[[0, 0, 1], [0, 1, 0]],[[0, 1, 1], [0, 0, 1]]])
    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    num_choices = 2

    model = BertForMultipleChoice(config, num_choices)
    logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config, num_choices):
        super(BertForMultipleChoice, self).__init__(config)
        self.num_choices = num_choices
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, 1)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, labels=None):
        flat_input_ids = input_ids.view(-1, input_ids.size(-1))
        flat_token_type_ids = token_type_ids.view(-1, token_type_ids.size(-1))
        flat_attention_mask = attention_mask.view(-1, attention_mask.size(-1))
        _, pooled_output = self.bert(flat_input_ids, flat_token_type_ids, flat_attention_mask, output_all_encoded_layers=False)
        pooled_output = self.dropout(pooled_output)
        logits = self.classifier(pooled_output)
        reshaped_logits = logits.view(-1, self.num_choices)

        if labels is not None:
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(reshaped_logits, labels)
            return loss
        else:
            return reshaped_logits


class BertForTokenClassification(BertPreTrainedModel):
    """BERT model for token-level classification.
    This module is composed of the BERT model with a linear layer on top of
    the full hidden state of the last layer.

    Params:
        `config`: a BertConfig class instance with the configuration to build a new model.
        `num_labels`: the number of classes for the classifier. Default = 2.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `labels`: labels for the classification output: torch.LongTensor of shape [batch_size, sequence_length]
            with indices selected in [0, ..., num_labels].

    Outputs:
        if `labels` is not `None`:
            Outputs the CrossEntropy classification loss of the output with the labels.
        if `labels` is `None`:
            Outputs the classification logits of shape [batch_size, sequence_length, num_labels].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    num_labels = 2

    model = BertForTokenClassification(config, num_labels)
    logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config, num_labels):
        super(BertForTokenClassification, self).__init__(config)
        self.num_labels = num_labels
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, num_labels)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, labels=None):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        if labels is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            if attention_mask is not None:
                active_loss = attention_mask.view(-1) == 1
                active_logits = logits.view(-1, self.num_labels)[active_loss]
                active_labels = labels.view(-1)[active_loss]
                loss = loss_fct(active_logits, active_labels)
            else:
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            return loss
        else:
            return logits
    

        
class BertForQuestionAnswering(BertPreTrainedModel):
    """BERT model for Question Answering (span extraction).
    This module is composed of the BERT model with a linear layer on top of
    the sequence output that computes start_logits and end_logits

    Params:
        `config`: a BertConfig class instance with the configuration to build a new model.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `start_positions`: position of the first token for the labeled span: torch.LongTensor of shape [batch_size].
            Positions are clamped to the length of the sequence and position outside of the sequence are not taken
            into account for computing the loss.
        `end_positions`: position of the last token for the labeled span: torch.LongTensor of shape [batch_size].
            Positions are clamped to the length of the sequence and position outside of the sequence are not taken
            into account for computing the loss.

    Outputs:
        if `start_positions` and `end_positions` are not `None`:
            Outputs the total_loss which is the sum of the CrossEntropy loss for the start and end token positions.
        if `start_positions` or `end_positions` is `None`:
            Outputs a tuple of start_logits, end_logits which are the logits respectively for the start and end
            position tokens of shape [batch_size, sequence_length].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    model = BertForQuestionAnswering(config)
    start_logits, end_logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config):
        super(BertForQuestionAnswering, self).__init__(config)
        self.bert = BertModel(config)
        # TODO check with Google if it's normal there is no dropout on the token classifier of SQuAD in the TF version
        # self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.qa_outputs = nn.Linear(config.hidden_size, 2)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, start_positions=None, end_positions=None):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        logits = self.qa_outputs(sequence_output)
        start_logits, end_logits = logits.split(1, dim=-1)
        start_logits = start_logits.squeeze(-1)
        end_logits = end_logits.squeeze(-1)

        if start_positions is not None and end_positions is not None:
            # If we are on multi-GPU, split add a dimension
            if len(start_positions.size()) > 1:
                start_positions = start_positions.squeeze(-1)
            if len(end_positions.size()) > 1:
                end_positions = end_positions.squeeze(-1)
            # sometimes the start/end positions are outside our model inputs, we ignore these terms
            ignored_index = start_logits.size(1)
            start_positions.clamp_(0, ignored_index)
            end_positions.clamp_(0, ignored_index)

            loss_fct = CrossEntropyLoss(ignore_index=ignored_index)
            start_loss = loss_fct(start_logits, start_positions)
            end_loss = loss_fct(end_logits, end_positions)
            total_loss = (start_loss + end_loss) / 2
            return total_loss
        else:
            return start_logits, end_logits

        
class BertForSimpleRelationExtraction(BertPreTrainedModel):

    def __init__(self, config, rel_dim):
        super(BertForSimpleRelationExtraction, self).__init__(config)
        self.rel_dim = rel_dim
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, rel_dim)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, kb_rel=None):
        sequence_output, pooled_output = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output = self.dropout(pooled_output)
        logits = self.classifier(pooled_output)
        reshaped_logits = logits.view(-1, self.rel_dim)
        loss_fct = MSELoss()
        loss = loss_fct(reshaped_logits, kb_rel)
        return loss
        '''
        if kb_rel is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            if attention_mask is not None:
                active_loss = attention_mask.view(-1) == 1
                active_logits = logits.view(-1, self.rel_dim)[active_loss]
                active_kb_rel = kb_rel.view(-1)[active_loss]
                loss = loss_fct(active_logits, active_kb_rel)
            else:
                loss = loss_fct(logits.view(-1, self.rel_dim), kb_rel.view(-1))
            return loss
        else:
            return logits
        '''

class BertForSimpleRelationExtraction_allwords(BertPreTrainedModel):

    def __init__(self, config, rel_dim, max_tok):
        super(BertForSimpleRelationExtraction_allwords, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.encode_1 = nn.Linear(config.hidden_size, rel_dim)
        self.encode_2 = nn.Linear(rel_dim * max_tok, rel_dim)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, kb_rel=None):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output = self.dropout(sequence_output)
        logits = self.encode_1(sequence_output)
        #logits = self.dropout(logits)
        reshaped_logits = logits.view(-1, self.rel_dim * self.max_tok)
        logits_200 = self.encode_2(reshaped_logits)
        loss_fct = MSELoss()
        loss = loss_fct(logits_200, kb_rel)
        return loss, logits_200

class BertForSimpleRelationExtraction_allwords_posemb(BertPreTrainedModel):

    def __init__(self, config, rel_dim, max_tok, pos_dim):
        super(BertForSimpleRelationExtraction_allwords_posemb, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.pos_dim = pos_dim
        self.bert = BertModel(config)
        #self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.dropout = nn.Dropout(0.2)
        self.encode_1 = nn.Linear(config.hidden_size + pos_dim * 2, rel_dim)
        self.encode_2 = nn.Linear(rel_dim * max_tok, rel_dim)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, kb_rel=None, pos_tensor1 = None, pos_tensor2 = None):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output = self.dropout(sequence_output)
        seq_pos_attched = torch.cat([sequence_output, pos_tensor1,pos_tensor2], dim=2)
        logits = self.encode_1(seq_pos_attched)
        logits = self.dropout(logits)
        reshaped_logits = logits.view(-1, self.rel_dim * self.max_tok)
        logits_200 = self.encode_2(reshaped_logits)
        #logits_200 = self.dropout(logits_200)
        loss_fct = MSELoss()
        loss = loss_fct(logits_200, kb_rel)
        return loss

class BertForEntityandRelation_sep(BertPreTrainedModel):

    def __init__(self, config, rel_dim, max_tok):
        super(BertForEntityandRelation_sep, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.config_hiddensize = config.hidden_size
        
        self.encode_rel = nn.Linear(config.hidden_size * max_tok, rel_dim)
        self.encode_ent = nn.Linear(config.hidden_size, 1)
        
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, kb_rel=None, OIE_ent = None):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output = self.dropout(sequence_output)
        reshaped_logits = sequence_output.view(-1, self.config_hiddensize * self.max_tok)
        logits_rel = self.encode_rel(reshaped_logits)
        logits_ent = self.encode_ent(sequence_output)
        logits_ent = logits_ent.view(-1, self.max_tok)
        
        loss_fct_rel = MSELoss()
        loss_fct_ent = MultiLabelSoftMarginLoss()
        loss_rel = loss_fct_rel(logits_rel, kb_rel)
        loss_ent = loss_fct_ent(logits_ent, OIE_ent)
        return loss_rel,loss_ent

class BertForEntityandRelation_relthenent(BertPreTrainedModel):

    def __init__(self, config, rel_dim, max_tok):
        super(BertForEntityandRelation_relthenent, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.dropout2 = nn.Dropout(config.hidden_dropout_prob)
        self.dropout3 = nn.Dropout(config.hidden_dropout_prob)
        self.config_hiddensize = config.hidden_size
        
        self.encode_rel_1 = nn.Linear(config.hidden_size, rel_dim)
        self.encode_rel_2 = nn.Linear(rel_dim * max_tok, rel_dim)
        
        #self.encode_rel = nn.Linear(config.hidden_size * max_tok, rel_dim)
        self.encode_ent = nn.Linear(config.hidden_size + rel_dim, 1)
        
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, kb_rel=None, OIE_ent = None):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output = self.dropout(sequence_output)
        
        logits_rel = self.encode_rel_1(sequence_output)
        reshaped_logits = logits_rel.view(-1, self.rel_dim * self.max_tok)
        reshaped_logits = self.dropout2(reshaped_logits)
        logits_rel = self.encode_rel_2(reshaped_logits)
        
        #logits_rel = self.encode_rel(reshaped_logits)
        rel_to_ent = logits_rel.repeat(1,self.max_tok).view(-1,self.max_tok,self.rel_dim)
        ent_input = torch.cat((sequence_output, rel_to_ent),2)
        ent_input = self.dropout3(ent_input)
        logits_ent = self.encode_ent(ent_input)
        logits_ent = logits_ent.view(-1, self.max_tok)
        
        loss_fct_rel = MSELoss()
        loss_fct_ent = MultiLabelSoftMarginLoss()
        loss_rel = loss_fct_rel(logits_rel, kb_rel)
        loss_ent = loss_fct_ent(logits_ent, OIE_ent)
        
        return loss_rel,loss_ent

class BertForEntityandRelation_entthenrel(BertPreTrainedModel):

    def __init__(self, config, rel_dim, max_tok):
        super(BertForEntityandRelation_entthenrel, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.dropout2 = nn.Dropout(config.hidden_dropout_prob)
        self.dropout3 = nn.Dropout(config.hidden_dropout_prob)
        self.config_hiddensize = config.hidden_size
        
        self.encode_rel_1 = nn.Linear(config.hidden_size + 5, rel_dim)
        self.encode_rel_2 = nn.Linear(rel_dim * max_tok, rel_dim)
        
        #self.encode_rel = nn.Linear(config.hidden_size * max_tok + 1, rel_dim)
        self.encode_ent = nn.Linear(config.hidden_size, 1)
        
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, kb_rel=None, OIE_ent = None):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output = self.dropout(sequence_output)
        
        logits_ent = self.encode_ent(sequence_output)
        logits_ent = logits_ent.view(-1, self.max_tok)
        
        #reshaped_logits = sequence_output.view(-1, self.config_hiddensize * self.max_tok)
        ent_to_rel = logits_ent.view(-1, self.max_tok,1)
        ent_to_rel = ent_to_rel.repeat(1,1,5).view(-1, self.max_tok, 5)

        rel_input = torch.cat((sequence_output, ent_to_rel),2)
        rel_input = self.dropout2(rel_input)
        logits_rel = self.encode_rel_1(rel_input)
        reshaped_logits = logits_rel.view(-1, self.rel_dim * self.max_tok)
        logits_rel = self.dropout3(reshaped_logits)
        logits_rel_2 = self.encode_rel_2(logits_rel)
        
        loss_fct_rel = MSELoss()
        loss_fct_ent = MultiLabelSoftMarginLoss()
        loss_rel = loss_fct_rel(logits_rel_2, kb_rel)
        loss_ent = loss_fct_ent(logits_ent, OIE_ent)
        return loss_rel,loss_ent

class BertForEntityandRelation_entrelsametime(BertPreTrainedModel):

    def __init__(self, config, rel_dim, max_tok):
        super(BertForEntityandRelation_entrelsametime, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.dropout2 = nn.Dropout(config.hidden_dropout_prob)
        self.dropout3 = nn.Dropout(config.hidden_dropout_prob)
        self.config_hiddensize = config.hidden_size
        
        self.encode_rel_1 = nn.Linear(config.hidden_size, rel_dim)
        self.encode_rel_2 = nn.Linear((rel_dim+5) * max_tok, rel_dim)
        
        #self.encode_rel = nn.Linear(config.hidden_size * max_tok, rel_dim)
        self.encode_ent = nn.Linear(config.hidden_size, 1)
        
        #self.encode_rel_2 = nn.Linear(config.hidden_size * max_tok + 1, rel_dim)
        self.encode_ent_2 = nn.Linear(config.hidden_size + rel_dim, 1)
        
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, kb_rel=None, OIE_ent = None):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output = self.dropout(sequence_output)
        
        logits_rel = self.encode_rel_1(sequence_output)
        
        logits_ent = self.encode_ent(sequence_output)
        logits_ent = logits_ent.view(-1, self.max_tok)
        
        ent_to_rel = logits_ent.view(-1, self.max_tok,1)
        ent_to_rel = ent_to_rel.repeat(1,1,5).view(-1, self.max_tok, 5)
        
        #logits_rel = self.encode_rel(reshaped_logits)
        #logits_ent = self.encode_ent(sequence_output)
        #logits_ent = logits_ent.view(-1, self.max_tok)
        
        #rel_input = rel_input.view(-1, (self.rel_dim + 5) * self.max_tok)
        
        rel_input = torch.cat((logits_rel, ent_to_rel),2)
        rel_input = rel_input.view(-1, (self.rel_dim+5) * self.max_tok)
        rel_input = self.dropout2(rel_input)
        
        ent_input = torch.cat((sequence_output, logits_rel),2)
        ent_input = self.dropout3(ent_input)
        
        logits_rel_2 = self.encode_rel_2(rel_input)
        logits_ent_2 = self.encode_ent_2(ent_input)
        logits_ent_2 = logits_ent_2.view(-1, self.max_tok)
        
        loss_fct_rel = MSELoss()
        loss_fct_ent = MultiLabelSoftMarginLoss()
        loss_rel = loss_fct_rel(logits_rel_2, kb_rel)
        loss_ent = loss_fct_ent(logits_ent_2, OIE_ent)
        return loss_rel,loss_ent

class BertForEntityandRelation_entrelsametime_ver2(BertPreTrainedModel):

    def __init__(self, config, rel_dim, max_tok):
        super(BertForEntityandRelation_entrelsametime_ver2, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.dropout2 = nn.Dropout(config.hidden_dropout_prob)
        self.dropout3 = nn.Dropout(config.hidden_dropout_prob)
        self.config_hiddensize = config.hidden_size
        
        self.encode_rel_1 = nn.Linear(config.hidden_size, rel_dim)
        self.encode_rel_2 = nn.Linear((rel_dim+5) * max_tok, rel_dim)
        
        self.encode_ent = nn.Linear(config.hidden_size * max_tok, max_tok)
        self.encode_ent_2 = nn.Linear((config.hidden_size + rel_dim) * max_tok, max_tok)
        
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, kb_rel=None, OIE_ent = None):
        sequence_output_, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output_dr = self.dropout(sequence_output_)
        
        logits_rel = self.encode_rel_1(sequence_output_dr)
        
        
        re_sequence_output = sequence_output_dr.view(-1,self.max_tok * self.config_hiddensize)
        logits_ent = self.encode_ent(re_sequence_output)
        logits_ent = logits_ent.view(-1, self.max_tok)
        
        ent_to_rel = logits_ent.view(-1, self.max_tok,1)
        ent_to_rel = ent_to_rel.repeat(1,1,5).view(-1, self.max_tok, 5)
        
        rel_input = torch.cat((logits_rel, ent_to_rel),2)
        rel_input = rel_input.view(-1, (self.rel_dim+5) * self.max_tok)
        rel_input = self.dropout2(rel_input)
        
        ent_input = torch.cat((sequence_output_dr, logits_rel),2)
        ent_input = ent_input.view(-1, (self.config_hiddensize + self.rel_dim) * self.max_tok)
        ent_input = self.dropout3(ent_input)
        
        logits_rel_2 = self.encode_rel_2(rel_input)
        logits_ent_2 = self.encode_ent_2(ent_input)
        logits_ent_2 = logits_ent_2.view(-1, self.max_tok)
        
        loss_fct_rel = MSELoss()
        loss_fct_ent = MultiLabelSoftMarginLoss()
        loss_rel = loss_fct_rel(logits_rel_2, kb_rel)
        loss_ent = loss_fct_ent(logits_ent_2, OIE_ent)
        #testing
        #return loss_rel,loss_ent
        return logits_rel_2, logits_ent_2, sequence_output_

class BertForEntityandRelation_entrelsametime_ver2_negsen(BertPreTrainedModel):

    def __init__(self, config, rel_dim, max_tok):
        super(BertForEntityandRelation_entrelsametime_ver2_negsen, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.dropout2 = nn.Dropout(config.hidden_dropout_prob)
        self.dropout3 = nn.Dropout(config.hidden_dropout_prob)
        self.config_hiddensize = config.hidden_size
        
        self.encode_rel_1 = nn.Linear(config.hidden_size, rel_dim)
        self.encode_rel_2 = nn.Linear((rel_dim+5) * max_tok, rel_dim)
        
        self.encode_ent = nn.Linear(config.hidden_size * max_tok, max_tok)
        self.encode_ent_2 = nn.Linear((config.hidden_size + rel_dim) * max_tok, max_tok)
        
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, kb_rel=None, OIE_ent = None, negflag = None):
        sequence_output_, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output_dr = self.dropout(sequence_output_)
        
        logits_rel = self.encode_rel_1(sequence_output_dr)
        
        
        re_sequence_output = sequence_output_dr.view(-1,self.max_tok * self.config_hiddensize)
        logits_ent = self.encode_ent(re_sequence_output)
        logits_ent = logits_ent.view(-1, self.max_tok)
        
        ent_to_rel = logits_ent.view(-1, self.max_tok,1)
        ent_to_rel = ent_to_rel.repeat(1,1,5).view(-1, self.max_tok, 5)
        
        rel_input = torch.cat((logits_rel, ent_to_rel),2)
        rel_input = rel_input.view(-1, (self.rel_dim+5) * self.max_tok)
        rel_input = self.dropout2(rel_input)
        
        ent_input = torch.cat((sequence_output_dr, logits_rel),2)
        ent_input = ent_input.view(-1, (self.config_hiddensize + self.rel_dim) * self.max_tok)
        ent_input = self.dropout3(ent_input)
        
        logits_rel_2 = self.encode_rel_2(rel_input)
        logits_ent_2 = self.encode_ent_2(ent_input)
        logits_ent_2 = logits_ent_2.view(-1, self.max_tok)

        #loss_fct_rel = MSELoss()
        loss_fct_ent = MultiLabelSoftMarginLoss()
        #kb_rel = kb_rel * negflag
        #loss_rel = loss_fct_rel(logits_rel_2, kb_rel)
        loss_ent = loss_fct_ent(logits_ent_2, OIE_ent)
        #testing
        #return loss_rel,loss_ent
        return logits_rel_2, loss_ent

class BertForEntityandRelation_entrelsametime_ver2_negsen_last(BertPreTrainedModel):

    def __init__(self, config, rel_dim, max_tok):
        super(BertForEntityandRelation_entrelsametime_ver2_negsen_last, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.dropout2 = nn.Dropout(config.hidden_dropout_prob)
        self.dropout3 = nn.Dropout(config.hidden_dropout_prob)
        self.config_hiddensize = config.hidden_size
        
        self.encode_rel_1 = nn.Linear(config.hidden_size, rel_dim)
        self.encode_rel_2 = nn.Linear((rel_dim+5) * max_tok, rel_dim)
        
        self.encode_ent = nn.Linear(config.hidden_size * max_tok, max_tok)
        self.encode_ent_2 = nn.Linear((config.hidden_size + rel_dim) * max_tok, max_tok)
        
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, kb_rel=None, OIE_ent = None, negflag = None):
        sequence_output_, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=True)
        sequence_output_ = sequence_output_[10]
        sequence_output_dr = self.dropout(sequence_output_)
        
        logits_rel = self.encode_rel_1(sequence_output_dr)
        
        
        re_sequence_output = sequence_output_dr.view(-1,self.max_tok * self.config_hiddensize)
        logits_ent = self.encode_ent(re_sequence_output)
        logits_ent = logits_ent.view(-1, self.max_tok)
        
        ent_to_rel = logits_ent.view(-1, self.max_tok,1)
        ent_to_rel = ent_to_rel.repeat(1,1,5).view(-1, self.max_tok, 5)
        
        rel_input = torch.cat((logits_rel, ent_to_rel),2)
        rel_input = rel_input.view(-1, (self.rel_dim+5) * self.max_tok)
        rel_input = self.dropout2(rel_input)
        
        ent_input = torch.cat((sequence_output_dr, logits_rel),2)
        ent_input = ent_input.view(-1, (self.config_hiddensize + self.rel_dim) * self.max_tok)
        ent_input = self.dropout3(ent_input)
        
        logits_rel_2 = self.encode_rel_2(rel_input)
        logits_ent_2 = self.encode_ent_2(ent_input)
        logits_ent_2 = logits_ent_2.view(-1, self.max_tok)
        #testing
        #loss_fct_rel = MSELoss()
        loss_fct_ent = MultiLabelSoftMarginLoss()
        #kb_rel = kb_rel * negflag
        #loss_rel = loss_fct_rel(logits_rel_2, kb_rel)
        loss_ent = loss_fct_ent(logits_ent_2, OIE_ent)
        #testing
        #return loss_rel,loss_ent
        return logits_rel_2, loss_ent

class BertForEntityandRelation_ver3_negsen_minie_simple(BertPreTrainedModel):

    def __init__(self, config, rel_dim, max_tok):
        super(BertForEntityandRelation_ver3_negsen_minie_simple, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.dropout2 = nn.Dropout(config.hidden_dropout_prob)
        self.dropout3 = nn.Dropout(config.hidden_dropout_prob)
        self.config_hiddensize = config.hidden_size
        
        self.encode_rel_emb = nn.Linear(config.hidden_size * max_tok, rel_dim)
        
        self.encode_relent = nn.Linear(config.hidden_size * max_tok, max_tok*2)
        
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, kb_rel=None, OIE_ent = None, OIE_rel = None, negflag = None):
        sequence_output_, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=True)
        sequence_output_ = sequence_output_[10]
        sequence_output_dr = self.dropout(sequence_output_)
        
        re_sequence_output = sequence_output_dr.view(-1,self.max_tok * self.config_hiddensize)
        logits_rel = self.encode_rel_emb(re_sequence_output)
        
        logits_ent = self.encode_relent(re_sequence_output)
        logits_ent = logits_ent.view(-1, 2, self.max_tok)
        
        logits_ent, logits_reltext = torch.split(logits_ent, 1, dim=1)
        
        loss_fct_rel = MSELoss()
        loss_fct_ent = MultiLabelSoftMarginLoss()
        loss_fct_reltext = MultiLabelSoftMarginLoss()
        #kb_rel = kb_rel * negflag
        loss_rel = loss_fct_rel(logits_rel, kb_rel)
        loss_ent = loss_fct_ent(logits_ent, OIE_ent)
        loss_rel_text = loss_fct_reltext(logits_reltext, OIE_rel)
        #testing
        return logits_rel, logits_ent, logits_reltext
        #return loss_rel, loss_ent, loss_rel_text
    
class BertForEntityandRelation_ver3_negsen_minie_complex(BertPreTrainedModel):

    def __init__(self, config, rel_dim, max_tok):
        super(BertForEntityandRelation_ver3_negsen_minie_complex, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.dropout2 = nn.Dropout(config.hidden_dropout_prob)
        self.dropout3 = nn.Dropout(config.hidden_dropout_prob)
        self.dropout4 = nn.Dropout(config.hidden_dropout_prob)
        self.config_hiddensize = config.hidden_size
        
        self.encode_rel_1 = nn.Linear(config.hidden_size, rel_dim)
        self.encode_rel_2 = nn.Linear((rel_dim+10) * max_tok, rel_dim)
        
        self.encode_ent = nn.Linear(config.hidden_size * max_tok, max_tok)
        self.encode_ent_2 = nn.Linear((config.hidden_size + rel_dim) * max_tok, max_tok)
        
        self.encode_reltext = nn.Linear(config.hidden_size * max_tok, max_tok)
        self.encode_reltext_2 = nn.Linear((config.hidden_size + rel_dim) * max_tok, max_tok)
        
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, kb_rel=None, OIE_ent = None, OIE_rel = None, negflag = None):
        sequence_output_, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=True)
        sequence_output_ = sequence_output_[10]
        sequence_output_dr = self.dropout(sequence_output_)
        
        logits_rel = self.encode_rel_1(sequence_output_dr)
        
        re_sequence_output = sequence_output_dr.view(-1,self.max_tok * self.config_hiddensize)
        
        logits_ent = self.encode_ent(re_sequence_output)
        logits_ent = logits_ent.view(-1, self.max_tok)
        ent_to_rel = logits_ent.view(-1, self.max_tok,1)
        ent_to_rel = ent_to_rel.repeat(1,1,5).view(-1, self.max_tok, 5)
        
        logits_reltext = self.encode_reltext(re_sequence_output)
        logits_reltext = logits_reltext.view(-1, self.max_tok)
        reltext_to_rel = logits_reltext.view(-1, self.max_tok,1)
        reltext_to_rel = reltext_to_rel.repeat(1,1,5).view(-1, self.max_tok, 5)
        
        rel_input = torch.cat((logits_rel, ent_to_rel, reltext_to_rel),2)
        rel_input = rel_input.view(-1, (self.rel_dim+10) * self.max_tok)
        rel_input = self.dropout2(rel_input)
        
        ent_input = torch.cat((sequence_output_dr, logits_rel),2)
        ent_input = ent_input.view(-1, (self.config_hiddensize + self.rel_dim) * self.max_tok)
        ent_input = self.dropout3(ent_input)
        
        reltext_input = torch.cat((sequence_output_dr, logits_rel),2)
        reltext_input = reltext_input.view(-1, (self.config_hiddensize + self.rel_dim) * self.max_tok)
        reltext_input = self.dropout4(reltext_input)
        
        logits_rel_2 = self.encode_rel_2(rel_input)
        logits_ent_2 = self.encode_ent_2(ent_input)
        logits_ent_2 = logits_ent_2.view(-1, self.max_tok)
        logits_reltext_2 = self.encode_reltext_2(reltext_input)
        logits_reltext_2 = logits_reltext_2.view(-1, self.max_tok)

        loss_fct_rel = MSELoss()
        loss_fct_ent = MultiLabelSoftMarginLoss()
        loss_fct_reltext = MultiLabelSoftMarginLoss()
        #kb_rel = kb_rel * negflag
        loss_rel = loss_fct_rel(logits_rel_2, kb_rel)
        loss_ent = loss_fct_ent(logits_ent_2, OIE_ent)
        loss_reltext = loss_fct_reltext(logits_ent_2, OIE_rel)
        #testing
        #return loss_rel,loss_ent, loss_reltext
        return logits_rel_2, logits_ent_2, logits_reltext_2
    
class BertForQArelation(BertPreTrainedModel):

    def __init__(self, config, rel_dim, max_tok):
        super(BertForQArelation, self).__init__(config)
        self.max_tok = max_tok
        self.bert = BertModel(config)
        self.rel_dim = rel_dim
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.config_hiddensize = config.hidden_size
        
        self.rel_first = nn.Linear(config.hidden_size * max_tok, rel_dim)
        self.rel_second = nn.Linear(rel_dim, 2)
        
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, answer_label=None):
        sequence_output_, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=True)
        sequence_output_ = sequence_output_[10]
        sequence_output_dr = self.dropout(sequence_output_)
        
        re_sequence_output = sequence_output_dr.view(-1,self.max_tok * self.config_hiddensize)
        
        logits_rel = self.rel_first(re_sequence_output)
        logits_rel = logits_rel.view(-1, self.rel_dim)
        logits_rel = self.rel_second(logits_rel)
        logits_rel = logits_rel.view(-1, 2)
        
        #contrative loss function과 그냥 cross entropy 시도해보기
        
        loss_fct_rel = CrossEntropyLoss()
        loss_rel = loss_fct_rel(logits_rel, answer_label.view(-1))
        
        return loss_rel
        #test
        #return logits_rel

class BertForVer3_siamese_main_once(BertPreTrainedModel):
#PCNN 이용해서 rel 부분에 집중하기
#어텐션 적용 . 그냥 어텐션/multi head attention
    def __init__(self, config, rel_dim, max_tok):
        super(BertForVer3_siamese_main_once, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.dropout2 = nn.Dropout(config.hidden_dropout_prob)
        self.dropout3 = nn.Dropout(config.hidden_dropout_prob)
        self.config_hiddensize = config.hidden_size
        
        self.encode_rel_1 = nn.Linear(config.hidden_size, rel_dim)
        self.encode_rel_2 = nn.Linear((rel_dim+5) * max_tok, rel_dim)
        
        self.encode_ent = nn.Linear(config.hidden_size * max_tok, max_tok)
        self.encode_ent_2 = nn.Linear((config.hidden_size + rel_dim) * max_tok, max_tok)
        
        self.apply(self.init_bert_weights)

    def forward_once(self, input_ids, token_type_ids=None, attention_mask=None):
        sequence_output_, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output_dr = self.dropout(sequence_output_)
        
        logits_rel = self.encode_rel_1(sequence_output_dr)
        
        
        re_sequence_output = sequence_output_dr.view(-1,self.max_tok * self.config_hiddensize)
        logits_ent = self.encode_ent(re_sequence_output)
        logits_ent = logits_ent.view(-1, self.max_tok)
        
        ent_to_rel = logits_ent.view(-1, self.max_tok,1)
        ent_to_rel = ent_to_rel.repeat(1,1,5).view(-1, self.max_tok, 5)
        
        rel_input = torch.cat((logits_rel, ent_to_rel),2)
        rel_input = rel_input.view(-1, (self.rel_dim+5) * self.max_tok)
        rel_input = self.dropout2(rel_input)
        
        ent_input = torch.cat((sequence_output_dr, logits_rel),2)
        ent_input = ent_input.view(-1, (self.config_hiddensize + self.rel_dim) * self.max_tok)
        ent_input = self.dropout3(ent_input)
        
        logits_rel_2 = self.encode_rel_2(rel_input)
        logits_ent_2 = self.encode_ent_2(ent_input)
        logits_ent_2 = logits_ent_2.view(-1, self.max_tok)
        
        return logits_rel_2, logits_ent_2
    
    def forward(self, input_ids, input_ids_2, token_type_ids=None, attention_mask=None, token_type_ids_2=None, attention_mask_2=None, OIE_ent_1 = None, OIE_ent_2 = None, kb_rel_1=None, kb_rel_2 = None):
        rel_emb_1, ent_label_1 = self.forward_once(input_ids, token_type_ids, attention_mask)
        rel_emb_2, ent_label_2 = self.forward_once(input_ids_2, token_type_ids_2, attention_mask_2)
        return rel_emb_1, rel_emb_2
        loss_fct_ent = MultiLabelSoftMarginLoss()
        loss_ent_1 = loss_fct_ent(ent_label_1, OIE_ent_1)
        loss_ent_2 = loss_fct_ent(ent_label_2, OIE_ent_2)
        loss_ent = loss_ent_1 + loss_ent_2
        
        if (kb_rel_2 is None): # QA
            
            loss_fct_rel = ContrastiveLoss_new(margin = 4.0)
            loss_rel = loss_fct_rel(rel_emb_1,rel_emb_2,kb_rel_1.view(-1))
            
        else: # KB emb
            
            loss_fct_rel = MSELoss()
            loss_rel = loss_fct_rel(rel_emb_1,kb_rel_1) + loss_fct_rel(rel_emb_2,kb_rel_2)
        
        #return loss_rel, loss_ent
    
class BertForVer3_siamese_main_once_intraatt(BertPreTrainedModel):
#PCNN 이용해서 rel 부분에 집중하기
#어텐션 적용 . 그냥 어텐션/multi head attention
    def __init__(self, config, rel_dim, max_tok):
        super(BertForVer3_siamese_main_once_intraatt, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.dropout2 = nn.Dropout(config.hidden_dropout_prob)
        self.dropout3 = nn.Dropout(config.hidden_dropout_prob)
        self.config_hiddensize = config.hidden_size
        
        self.encode_rel_1 = nn.Linear(config.hidden_size, rel_dim)
        self.encode_rel_2 = nn.Linear((rel_dim+5) * max_tok, rel_dim)
        
        self.encode_ent = nn.Linear(config.hidden_size * max_tok, max_tok)
        self.encode_ent_2 = nn.Linear((config.hidden_size + rel_dim) * max_tok, max_tok)
        
        self.query = nn.Linear(rel_dim, rel_dim)
        self.key = nn.Linear(rel_dim, rel_dim)
        self.value = nn.Linear(rel_dim, rel_dim) #nomal attention projection
        
        self.apply(self.init_bert_weights)

    def forward_once(self, input_ids, token_type_ids=None, attention_mask=None):
        sequence_output_, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output_dr = self.dropout(sequence_output_)
        
        logits_rel = self.encode_rel_1(sequence_output_dr)
        
        
        re_sequence_output = sequence_output_dr.view(-1,self.max_tok * self.config_hiddensize)
        logits_ent = self.encode_ent(re_sequence_output)
        logits_ent = logits_ent.view(-1, self.max_tok)
        
        ent_to_rel = logits_ent.view(-1, self.max_tok,1)
        ent_to_rel = ent_to_rel.repeat(1,1,5).view(-1, self.max_tok, 5)
        
        rel_input = torch.cat((logits_rel, ent_to_rel),2)
        rel_input = rel_input.view(-1, (self.rel_dim+5) * self.max_tok)
        rel_input = self.dropout2(rel_input)
        
        ent_input = torch.cat((sequence_output_dr, logits_rel),2)
        ent_input = ent_input.view(-1, (self.config_hiddensize + self.rel_dim) * self.max_tok)
        ent_input = self.dropout3(ent_input)
        
        logits_rel_2 = self.encode_rel_2(rel_input)
        logits_ent_2 = self.encode_ent_2(ent_input)
        logits_ent_2 = logits_ent_2.view(-1, self.max_tok)
        
        return logits_rel_2, logits_ent_2
    
    def attention(self, rel_q, rel_a):
        query_projection = self.query(rel_q)
        key_projection = self.query(rel_a)
        value_projection = self.query(rel_a)
        
        att_score = torch.matmul(query_projection, key_projection.transpose(-1, -2))
        att_probs = nn.Softmax(dim=-1)(att_score)
        res_relemb = torch.matmul(att_probs, value_projection)
        
        return res_relemb
    
    def forward(self, input_ids, input_ids_2, token_type_ids=None, attention_mask=None, token_type_ids_2=None, attention_mask_2=None, OIE_ent_1 = None, OIE_ent_2 = None, kb_rel_1=None, kb_rel_2 = None, num_intrasen = None):
        
        loss_fct_ent = MultiLabelSoftMarginLoss()
        
        if (num_intrasen is None):
            
            rel_emb_1, ent_label_1 = self.forward_once(input_ids, token_type_ids, attention_mask)
            rel_emb_2, ent_label_2 = self.forward_once(input_ids_2, token_type_ids_2, attention_mask_2)
            #return rel_emb_1, rel_emb_2
            loss_ent_1 = loss_fct_ent(ent_label_1, OIE_ent_1)
            loss_ent_2 = loss_fct_ent(ent_label_2, OIE_ent_2)
            loss_ent = loss_ent_1 + loss_ent_2

            if (kb_rel_2 is None): # QA

                loss_fct_rel = ContrastiveLoss_new(margin = 4.0)
                loss_rel = loss_fct_rel(rel_emb_1,rel_emb_2,kb_rel_1.view(-1))

            else: # KB emb

                loss_fct_rel = MSELoss()
                loss_rel = loss_fct_rel(rel_emb_1,kb_rel_1) + loss_fct_rel(rel_emb_2,kb_rel_2)
        
        else:
            rel_emb_1, ent_label_1 = self.forward_once(input_ids, token_type_ids, attention_mask) # query
            loss_ent_1 = loss_fct_ent(ent_label_1, OIE_ent_1)
            
            batch_size = input_ids.shape[0]
            res_ent = [loss_ent_1]
            res_rel = []
            for i in range(batch_size):
                #one element in a batch can have several sentences
                rel_emb_2, ent_label_2 = self.forward_once(input_ids_2[i], token_type_ids_2[i], attention_mask_2[i])
                loss_ent_2 = loss_fct_ent(ent_label_2, OIE_ent_2[i])
                res_ent.append(loss_ent_2)
                res_relemb = self.attention(rel_emb_1[i], rel_emb_2)
                res_rel.append(res_relemb)
            
            res_rel = torch.stack(res_rel)
            loss_fct_rel = ContrastiveLoss_new(margin = 4.0)
            loss_rel = loss_fct_rel(rel_emb_1,res_rel,kb_rel_1.view(-1))
            loss_ent = (sum(res_ent) / len(res_ent)) * 2
            
        return loss_rel, loss_ent
    
class BertForVer3_siamese_sub(BertPreTrainedModel):
#PCNN 이용해서 rel 부분에 집중하기
    def __init__(self, config, rel_dim, max_tok):
        super(BertForVer3_siamese_sub, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.dropout2 = nn.Dropout(config.hidden_dropout_prob)
        self.dropout3 = nn.Dropout(config.hidden_dropout_prob)
        self.config_hiddensize = config.hidden_size
        
        self.encode_rel_1 = nn.Linear(config.hidden_size, rel_dim)
        self.encode_rel_2 = nn.Linear((rel_dim+5) * max_tok, rel_dim)
        
        self.encode_ent = nn.Linear(config.hidden_size * max_tok, max_tok)
        self.encode_ent_2 = nn.Linear((config.hidden_size + rel_dim) * max_tok, max_tok)
        
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None):
        sequence_output_, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output_dr = self.dropout(sequence_output_)
        
        logits_rel = self.encode_rel_1(sequence_output_dr)
        
        
        re_sequence_output = sequence_output_dr.view(-1,self.max_tok * self.config_hiddensize)
        logits_ent = self.encode_ent(re_sequence_output)
        logits_ent = logits_ent.view(-1, self.max_tok)
        
        ent_to_rel = logits_ent.view(-1, self.max_tok,1)
        ent_to_rel = ent_to_rel.repeat(1,1,5).view(-1, self.max_tok, 5)
        
        rel_input = torch.cat((logits_rel, ent_to_rel),2)
        rel_input = rel_input.view(-1, (self.rel_dim+5) * self.max_tok)
        rel_input = self.dropout2(rel_input)
        
        ent_input = torch.cat((sequence_output_dr, logits_rel),2)
        ent_input = ent_input.view(-1, (self.config_hiddensize + self.rel_dim) * self.max_tok)
        ent_input = self.dropout3(ent_input)
        
        logits_rel_2 = self.encode_rel_2(rel_input)
        logits_ent_2 = self.encode_ent_2(ent_input)
        logits_ent_2 = logits_ent_2.view(-1, self.max_tok)
        
        return logits_rel_2, logits_ent_2

class BertForVer3_siamese_main(BertPreTrainedModel):

    def __init__(self, config, rel_dim, max_tok):
        super(BertForVer3_siamese_main, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.submodel = BertForVer3_siamese_sub(config, rel_dim = self.rel_dim, max_tok = self.max_tok)
        self.config_hiddensize = config.hidden_size
        
        self.apply(self.init_bert_weights)
        
    def forward(self, input_ids, input_ids_2, token_type_ids=None, attention_mask=None, token_type_ids_2=None, attention_mask_2=None, OIE_ent_1 = None, OIE_ent_2 = None, kb_rel_1=None, kb_rel_2 = None):
        #1. MinIE ent label 필요, 2. contrastive loss function 필요 3. loss간 normalize 필요
        rel_emb_1, ent_label_1 = self.submodel(input_ids, token_type_ids, attention_mask)
        rel_emb_2, ent_label_2 = self.submodel(input_ids_2, token_type_ids_2, attention_mask_2)
        return rel_emb_1,rel_emb_2
        loss_fct_ent = MultiLabelSoftMarginLoss()
        loss_ent_1 = loss_fct_ent(ent_label_1, OIE_ent_1)
        loss_ent_2 = loss_fct_ent(ent_label_2, OIE_ent_2)
        loss_ent = loss_ent_1 + loss_ent_2
        
        if (kb_rel_2 is None): # QA
            #cross entropy로 실험
            #negative example을 늘려서 실험
            #QA로만 실험
            #학습 과정에서의 loss 관찰하기 (각 epoch당 이나 각 mini batch, 각 case들)
            #kb 수가 절반이 되서 학습이 잘 안될 수 있으니 epoch 3까지 KB로만 학습하고 이후에 QA랑 KB 석어서 학습하기
            #마지막 layer만 사용해야함
            #neg loss가 pos loss에 비해 작은 편임. neg loss에 더 비중을 줄 필요 있음
            
            #neg loss가 굉장히 작음. 이유?
            #BERT sub랑 main 합쳐서 forword_once 형태로
            #각 모델에 들어가는 그래디안트값 확인하기 (sub까지 loss가 안가는 것일수도 있음)
            #contrastive loss 대신 abs 마이너스로 -> cross entropy에 넣기
            loss_fct_rel = ContrastiveLoss_new(margin = 2.0)
            loss_rel = loss_fct_rel(rel_emb_1,rel_emb_2,kb_rel_1.view(-1))
            
        else: # KB emb
            #cross entropy로 실험?
            loss_fct_rel = MSELoss()
            loss_rel = loss_fct_rel(rel_emb_1,kb_rel_1) + loss_fct_rel(rel_emb_2,kb_rel_2)
        
        #return loss_rel, loss_ent
        #test
        #return rel_emb_1, rel_emb_2

class BertForVer3_siamese_main_mse(BertPreTrainedModel):

    def __init__(self, config, rel_dim, max_tok):
        super(BertForVer3_siamese_main_mse, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.submodel = BertForVer3_siamese_sub(config, rel_dim = self.rel_dim, max_tok = self.max_tok)
        self.config_hiddensize = config.hidden_size
        
        self.apply(self.init_bert_weights)
        
    def forward(self, input_ids, input_ids_2, token_type_ids=None, attention_mask=None, token_type_ids_2=None, attention_mask_2=None, OIE_ent_1 = None, OIE_ent_2 = None, kb_rel_1=None, kb_rel_2 = None):
        #1. MinIE ent label 필요, 2. contrastive loss function 필요 3. loss간 normalize 필요
        rel_emb_1, ent_label_1 = self.submodel(input_ids, token_type_ids, attention_mask)
        rel_emb_2, ent_label_2 = self.submodel(input_ids_2, token_type_ids_2, attention_mask_2)
        #return rel_emb_1, rel_emb_2
        loss_fct_ent = MultiLabelSoftMarginLoss()
        loss_ent_1 = loss_fct_ent(ent_label_1, OIE_ent_1)
        loss_ent_2 = loss_fct_ent(ent_label_2, OIE_ent_2)
        loss_ent = loss_ent_1 + loss_ent_2
        
        if (kb_rel_2 is None): # QA
            #cross entropy로 실험
            #negative example을 늘려서 실험
            #QA로만 실험
            #학습 과정에서의 loss 관찰하기 (각 epoch당 이나 각 mini batch, 각 case들)
            #kb 수가 절반이 되서 학습이 잘 안될 수 있으니 epoch 3까지 KB로만 학습하고 이후에 QA랑 KB 석어서 학습하기
            rel_emb_2 = rel_emb_2 * kb_rel_1
            loss_fct_rel = MSELoss()
            loss_rel = loss_fct_rel(rel_emb_1,rel_emb_2)
            
        else: # KB emb
            #cross entropy로 실험?
            loss_fct_rel = MSELoss()
            loss_rel = (loss_fct_rel(rel_emb_1,kb_rel_1) + loss_fct_rel(rel_emb_2,kb_rel_2)) / 2
        
        return loss_rel, loss_ent
        #test
        #return rel_emb_1, rel_emb_2, loss_rel, loss_ent
    
class BertForVer3_siamese_main_cross(BertPreTrainedModel):

    def __init__(self, config, rel_dim, max_tok):
        super(BertForVer3_siamese_main_cross, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.submodel = BertForVer3_siamese_sub(config, rel_dim = self.rel_dim, max_tok = self.max_tok)
        self.config_hiddensize = config.hidden_size
        self.encode_rel2b = nn.Linear(rel_dim*2, 2)
        self.apply(self.init_bert_weights)
        
    def forward(self, input_ids, input_ids_2, token_type_ids=None, attention_mask=None, token_type_ids_2=None, attention_mask_2=None, OIE_ent_1 = None, OIE_ent_2 = None, kb_rel_1=None, kb_rel_2 = None):
        #1. MinIE ent label 필요, 2. contrastive loss function 필요 3. loss간 normalize 필요
        rel_emb_1, ent_label_1 = self.submodel(input_ids, token_type_ids, attention_mask)
        rel_emb_2, ent_label_2 = self.submodel(input_ids_2, token_type_ids_2, attention_mask_2)
        return rel_emb_1, rel_emb_2
        loss_fct_ent = MultiLabelSoftMarginLoss()
        loss_ent_1 = loss_fct_ent(ent_label_1, OIE_ent_1)
        loss_ent_2 = loss_fct_ent(ent_label_2, OIE_ent_2)
        loss_ent = loss_ent_1 + loss_ent_2
        
        if (kb_rel_2 is None): # QA
            rel_emb_cat = torch.cat((rel_emb_1, rel_emb_2),1)
            rel_emb_cat_s = self.encode_rel2b(rel_emb_cat)
            loss_fct_rel = CrossEntropyLoss()
            loss_rel = loss_fct_rel(rel_emb_cat_s,kb_rel_1.view(-1))
            
        else: # KB emb
            #cross entropy로 실험?
            loss_fct_rel = MSELoss()
            loss_rel = (loss_fct_rel(rel_emb_1,kb_rel_1) + loss_fct_rel(rel_emb_2,kb_rel_2)) / 2
        
        #return loss_rel, loss_ent
        #test
        #return rel_emb_1, rel_emb_2, loss_rel, loss_ent
        
class BertForEntityandRelation_entrelsametime_ver2_neg(BertPreTrainedModel):

    def __init__(self, config, rel_dim, max_tok):
        super(BertForEntityandRelation_entrelsametime_ver2_neg, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.dropout2 = nn.Dropout(config.hidden_dropout_prob)
        self.dropout3 = nn.Dropout(config.hidden_dropout_prob)
        self.config_hiddensize = config.hidden_size
        
        self.encode_rel_1 = nn.Linear(config.hidden_size, rel_dim)
        self.encode_rel_2 = nn.Linear((rel_dim+5) * max_tok, rel_dim)
        
        self.encode_ent = nn.Linear(config.hidden_size * max_tok, max_tok)
        self.encode_ent_2 = nn.Linear((config.hidden_size + rel_dim) * max_tok, max_tok)
        
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, kb_rel=None, OIE_ent = None, kb_rel_neg = None, kb_rel_neg_2 = None):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output = self.dropout(sequence_output)
        
        logits_rel = self.encode_rel_1(sequence_output)
        
        
        re_sequence_output = sequence_output.view(-1,self.max_tok * self.config_hiddensize)
        logits_ent = self.encode_ent(re_sequence_output)
        logits_ent = logits_ent.view(-1, self.max_tok)
        
        ent_to_rel = logits_ent.view(-1, self.max_tok,1)
        ent_to_rel = ent_to_rel.repeat(1,1,5).view(-1, self.max_tok, 5)
        
        rel_input = torch.cat((logits_rel, ent_to_rel),2)
        rel_input = rel_input.view(-1, (self.rel_dim+5) * self.max_tok)
        rel_input = self.dropout2(rel_input)
        
        ent_input = torch.cat((sequence_output, logits_rel),2)
        ent_input = ent_input.view(-1, (self.config_hiddensize + self.rel_dim) * self.max_tok)
        ent_input = self.dropout3(ent_input)
        
        logits_rel_2 = self.encode_rel_2(rel_input)
        logits_ent_2 = self.encode_ent_2(ent_input)
        logits_ent_2 = logits_ent_2.view(-1, self.max_tok)
        
        loss_fct_rel = MSELoss()
        loss_fct_ent = MultiLabelSoftMarginLoss()
        loss_rel = loss_fct_rel(logits_rel_2, kb_rel)
        loss_ent = loss_fct_ent(logits_ent_2, OIE_ent)
        
        #eval neg X
        #loss_rel_neg_1 = loss_fct_rel(logits_rel_2, kb_rel_neg)
        #loss_rel_neg_2 = loss_fct_rel(logits_rel_2, kb_rel_neg_2)
        #loss_rel_neg = (loss_rel_neg_1 + loss_rel_neg_2) / 2
        
        #if (loss_rel > loss_rel_neg):
            #loss_rel = loss_rel - loss_rel_neg
        #test
        #return loss_rel,loss_ent
        return logits_rel_2,logits_ent_2

class BertForEntityandRelation_sep_ver2(BertPreTrainedModel):

    def __init__(self, config, rel_dim, max_tok):
        super(BertForEntityandRelation_sep_ver2, self).__init__(config)
        self.rel_dim = rel_dim
        self.max_tok = max_tok
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.dropout2 = nn.Dropout(config.hidden_dropout_prob)
        self.config_hiddensize = config.hidden_size
        
        self.encode_rel_1 = nn.Linear(config.hidden_size, rel_dim)
        self.encode_rel_2 = nn.Linear(rel_dim * max_tok, rel_dim)
        
        self.encode_ent = nn.Linear(config.hidden_size, 1)
        
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, kb_rel=None, OIE_ent = None):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output = self.dropout(sequence_output)
        
        logits_rel = self.encode_rel_1(sequence_output)
        reshaped_logits = logits_rel.view(-1, self.rel_dim * self.max_tok)
        reshaped_logits = self.dropout(reshaped_logits)
        logits_rel = self.encode_rel_2(reshaped_logits)
        
        logits_ent = self.encode_ent(sequence_output)
        logits_ent = logits_ent.view(-1, self.max_tok)
        
        loss_fct_rel = MSELoss()
        loss_fct_ent = MultiLabelSoftMarginLoss()
        loss_rel = loss_fct_rel(logits_rel, kb_rel)
        loss_ent = loss_fct_ent(logits_ent, OIE_ent)
        return loss_rel,loss_ent