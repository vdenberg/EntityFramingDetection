from transformers import BertPreTrainedModel, BertModel
from torch.nn import Dropout, Linear
from torch.nn import CrossEntropyLoss, MSELoss
import torch
from torch import nn
from transformers.optimization import AdamW, get_linear_schedule_with_warmup
from torch.optim.lr_scheduler import CyclicLR
import os, pickle
import numpy as np
from lib.utils import get_torch_device, to_tensor, to_batches
from lib.evaluate.Eval import my_eval
from torch.nn import CrossEntropyLoss, MSELoss, Embedding, Dropout, Linear, Sigmoid, LSTM
from transformers.configuration_roberta import RobertaConfig
from transformers.modeling_roberta import RobertaModel, RobertaClassificationHead, ROBERTA_PRETRAINED_MODEL_ARCHIVE_MAP

# ssc
from allennlp.training.metrics import F1Measure, CategoricalAccuracy
from allennlp.modules import TextFieldEmbedder, TimeDistributed, Seq2SeqEncoder
from allennlp.modules.conditional_random_field import ConditionalRandomField
from allennlp.nn.util import get_text_field_mask

# helpers
class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self, my_id, input_ids, input_mask, segment_ids, label_id):
        self.my_id = my_id
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.label_id = label_id


def load_features(fp, batch_size):
    with open(fp, "rb") as f:
        ids, data, labels = to_tensor(pickle.load(f))
    batches = to_batches(data, batch_size=batch_size)
    return ids, batches, labels


#@add_start_docstrings("""RoBERTa Model transformer with a sequence classification/regression head on top (a linear layer
 #   on top of the pooled output) e.g. for GLUE tasks. """,
 #                     ROBERTA_START_DOCSTRING, ROBERTA_INPUTS_DOCSTRING)
class RobertaSSC(BertPreTrainedModel):
    r"""
        **labels**: (`optional`) ``torch.LongTensor`` of shape ``(batch_size,)``:
            Labels for computing the sequence classification/regression loss.
            Indices should be in ``[0, ..., config.num_labels]``.
            If ``config.num_labels == 1`` a regression loss is computed (Mean-Square loss),
            If ``config.num_labels > 1`` a classification loss is computed (Cross-Entropy).

    Outputs: `Tuple` comprising various elements depending on the configuration (config) and inputs:
        **loss**: (`optional`, returned when ``labels`` is provided) ``torch.FloatTensor`` of shape ``(1,)``:
            Classification (or regression if config.num_labels==1) loss.
        **logits**: ``torch.FloatTensor`` of shape ``(batch_size, config.num_labels)``
            Classification (or regression if config.num_labels==1) scores (before SoftMax).
        **hidden_states**: (`optional`, returned when ``config.output_hidden_states=True``)
            list of ``torch.FloatTensor`` (one for the output of each layer + the output of the embeddings)
            of shape ``(batch_size, sequence_length, hidden_size)``:
            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        **attentions**: (`optional`, returned when ``config.output_attentions=True``)
            list of ``torch.FloatTensor`` (one for each layer) of shape ``(batch_size, num_heads, sequence_length, sequence_length)``:
            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention heads.

    Examples::

        tokenizer = RobertaTokenizer.from_pretrained('roberta-base')
        model = RobertaForSequenceClassification.from_pretrained('roberta-base')
        input_ids = torch.tensor(tokenizer.encode("Hello, my dog is cute")).unsqueeze(0)  # Batch size 1
        labels = torch.tensor([1]).unsqueeze(0)  # Batch size 1
        outputs = model(input_ids, labels=labels)
        loss, logits = outputs[:2]

    """
    config_class = RobertaConfig
    pretrained_model_archive_map = ROBERTA_PRETRAINED_MODEL_ARCHIVE_MAP
    base_model_prefix = "roberta"

    def __init__(self, config):
        super(RobertaSSC, self).__init__(config)
        self.num_labels = config.num_labels

        self.roberta = RobertaModel(config)
        self.classifier = RobertaClassificationHead(config)
        self.sigm = nn.Sigmoid()

        ### ssc attributes
        self.use_sep = True
        self.with_crf = False
        self.sci_sum = False
        self.dropout = torch.nn.Dropout(p=0.1)

        # define loss
        if self.sci_sum:
            self.loss = torch.nn.MSELoss(reduction='none')  # labels are rouge scores
            self.labels_are_scores = True
            self.num_labels = 1
        else:
            self.loss = torch.nn.CrossEntropyLoss(ignore_index=-1, reduction='none')
            self.labels_are_scores = False
            self.num_labels = 2
            # define accuracy metrics
            self.label_accuracy = CategoricalAccuracy()
            self.label_f1_metrics = {}

            # define F1 metrics per label
            self.label_vocab = {0: 0, 1: 1}
            for label_index in range(self.num_labels):
                label_name = self.label_vocab[label_index]
                self.label_f1_metrics[label_name] = F1Measure(label_index)

        encoded_sentence_dim = 768

        ff_in_dim = encoded_sentence_dim #if self.use_sep else self_attn.get_output_dim()
        #ff_in_dim += self.additional_feature_size

        self.time_distributed_aggregate_feedforward = TimeDistributed(Linear(ff_in_dim, self.num_labels))

        if self.with_crf:
            self.crf = ConditionalRandomField(
                self.num_labels, constraints=None,
                include_start_end_transitions=True
            )

    def forward(self, input_ids=None, attention_mask=None, token_type_ids=None, position_ids=None, head_mask=None,
                inputs_embeds=None,
                labels=None, ssc=False):
        outputs = self.roberta(input_ids,
                               attention_mask=attention_mask,
                               token_type_ids=token_type_ids,
                               position_ids=position_ids,
                               head_mask=head_mask,
                               inputs_embeds=inputs_embeds)
        sequence_output = outputs[0]
        embedded_sentences = sequence_output  # torch.Size([6, 124, 768])

        #mask = get_text_field_mask(input_ids, num_wrapping_dims=1).float()
        print(input_ids)
        print(input_ids.shape)
        print(labels)
        print(labels.shape)
        #torch.Size([6, 124])
        #torch.Size([6])
        #batch_size, num_sentences, _, _ = embedded_sentences.size()
        batch_size, num_sentences, _ = embedded_sentences.size()

        if self.use_sep:
            # The following code collects vectors of the SEP tokens from all the examples in the batch,
            # and arrange them in one list. It does the same for the labels and confidences.
            # TODO: replace 103 with '[SEP]'
            #sentences_mask = sentences['bert'] == 103  # mask for all the SEP tokens in the batch
            sentences_mask = input_ids == 2  # mask for all the SEP tokens in the batch
            embedded_sentences = embedded_sentences[
                sentences_mask]  # given batch_size x num_sentences_per_example x sent_len x vector_len
            # returns num_sentences_per_batch x vector_len
            print(embedded_sentences.shape) # torch.Size([4, 768])
            assert embedded_sentences.dim() == 2
            num_sentences = embedded_sentences.shape[0]
            # for the rest of the code in this model to work, think of the data we have as one example
            # with so many sentences and a batch of size 1
            batch_size = 1
            embedded_sentences = embedded_sentences.unsqueeze(dim=0)
            embedded_sentences = self.dropout(embedded_sentences)

            if labels is not None:
                if self.labels_are_scores:
                    labels_mask = labels != 0.0  # mask for all the labels in the batch (no padding)
                else:
                    labels_mask = labels != -1  # mask for all the labels in the batch (no padding)

                labels = labels[
                    labels_mask]  # given batch_size x num_sentences_per_example return num_sentences_per_batch
                assert labels.dim() == 1

                num_labels = labels.shape[0]
                if num_labels != num_sentences:  # bert truncates long sentences, so some of the SEP tokens might be gone
                    assert num_labels > num_sentences  # but `num_labels` should be at least greater than `num_sentences`
                    #logger.warning(f'Found {num_labels} labels but {num_sentences} sentences')
                    labels = labels[:num_sentences]  # Ignore some labels. This is ok for training but bad for testing.
                    # We are ignoring this problem for now.
                    # TODO: fix, at least for testing

                # similar to `embedded_sentences`, add an additional dimension that corresponds to batch_size=1
                labels = labels.unsqueeze(dim=0)

        else:
            pass
            '''
            # ['CLS'] token
            embedded_sentences = embedded_sentences[:, :, 0, :]
            embedded_sentences = self.dropout(embedded_sentences)
            batch_size, num_sentences, _ = embedded_sentences.size()
            sent_mask = (mask.sum(dim=2) != 0)
            embedded_sentences = self.self_attn(embedded_sentences, sent_mask)
            '''

        label_logits = self.time_distributed_aggregate_feedforward(embedded_sentences)
        label_probs = torch.nn.functional.softmax(label_logits, dim=-1)

        logits = self.classifier(sequence_output)
        #probs = self.sigm(logits)

        if labels is not None:
            # Compute cross entropy loss
            flattened_logits = label_logits.view((batch_size * num_sentences), self.num_labels)
            flattened_gold = labels.contiguous().view(-1)

            if not self.with_crf:
                label_loss = self.loss(flattened_logits.squeeze(), flattened_gold)

                label_loss = label_loss.mean()
                flattened_probs = torch.softmax(flattened_logits, dim=-1)

            if not self.labels_are_scores:
                evaluation_mask = (flattened_gold != -1)
                self.label_accuracy(flattened_probs.float().contiguous(), flattened_gold.squeeze(-1), mask=evaluation_mask)

                # compute F1 per label
                for label_index in range(self.num_labels):
                    label_name = self.self.label_vocab[label_index]
                    metric = self.label_f1_metrics[label_name]
                    metric(flattened_probs, flattened_gold, mask=evaluation_mask)

        #outputs = (logits, probs, sequence_output) + outputs[2:]
        #outputs = (logits, label_probs, sequence_output) + outputs[2:]
        outputs = (label_logits, sequence_output) + outputs[2:]

        if labels is not None:
            if self.num_labels == 1:
                #  We are doing regression
                loss_fct = MSELoss()
                loss = loss_fct(logits.view(-1), labels.view(-1))
            else:
                loss_fct = CrossEntropyLoss()
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            outputs = (loss,) + outputs

        return outputs  # (loss), logits, (hidden_states), (attentions)


#@add_start_docstrings("""Roberta Model with a token classification head on top (a linear layer on top of
#    the hidden-states output) e.g. for Named-Entity-Recognition (NER) tasks. """,
#                      ROBERTA_START_DOCSTRING, ROBERTA_INPUTS_DOCSTRING)
class RobertaForTokenClassification(BertPreTrainedModel):
    r"""
        **labels**: (`optional`) ``torch.LongTensor`` of shape ``(batch_size, sequence_length)``:
            Labels for computing the token classification loss.
            Indices should be in ``[0, ..., config.num_labels - 1]``.

    Outputs: `Tuple` comprising various elements depending on the configuration (config) and inputs:
        **loss**: (`optional`, returned when ``labels`` is provided) ``torch.FloatTensor`` of shape ``(1,)``:
            Classification loss.
        **scores**: ``torch.FloatTensor`` of shape ``(batch_size, sequence_length, config.num_labels)``
            Classification scores (before SoftMax).
        **hidden_states**: (`optional`, returned when ``config.output_hidden_states=True``)
            list of ``torch.FloatTensor`` (one for the output of each layer + the output of the embeddings)
            of shape ``(batch_size, sequence_length, hidden_size)``:
            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        **attentions**: (`optional`, returned when ``config.output_attentions=True``)
            list of ``torch.FloatTensor`` (one for each layer) of shape ``(batch_size, num_heads, sequence_length, sequence_length)``:
            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention heads.

    Examples::

        tokenizer = RobertaTokenizer.from_pretrained('roberta-base')
        model = RobertaForTokenClassification.from_pretrained('roberta-base')
        input_ids = torch.tensor(tokenizer.encode("Hello, my dog is cute", add_special_tokens=True)).unsqueeze(0)  # Batch size 1
        labels = torch.tensor([1] * input_ids.size(1)).unsqueeze(0)  # Batch size 1
        outputs = model(input_ids, labels=labels)
        loss, scores = outputs[:2]

    """
    config_class = RobertaConfig
    pretrained_model_archive_map = ROBERTA_PRETRAINED_MODEL_ARCHIVE_MAP
    base_model_prefix = "roberta"

    def __init__(self, config):
        super(RobertaForTokenClassification, self).__init__(config)
        self.num_labels = config.num_labels

        self.roberta = RobertaModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.num_labels)

        self.init_weights()
        self.sigm = nn.Sigmoid()

    def forward(self, input_ids=None, attention_mask=None, token_type_ids=None,
                position_ids=None, head_mask=None, inputs_embeds=None, labels=None):

        outputs = self.roberta(input_ids,
                               attention_mask=attention_mask,
                               token_type_ids=token_type_ids,
                               position_ids=position_ids,
                               head_mask=head_mask,
                               inputs_embeds=inputs_embeds)
        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)
        probs = self.sigm(logits)

        outputs = (logits, probs, sequence_output) + outputs[2:]  # add hidden states and attention if they are here
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
            outputs = (loss,) + outputs

        return outputs  # (loss), scores, (hidden_states), (attentions)


class Inferencer():
    def __init__(self, reports_dir, logger, device, use_cuda):
        self.device = device
        self.reports_dir = reports_dir
        self.logger = logger
        self.device = device
        self.use_cuda = use_cuda

    def predict(self, model, data, return_embeddings=False, emb_type='poolbert', output_mode='classification'):
        model.to(self.device)
        model.eval()

        preds = []
        embeddings = []
        for step, batch in enumerate(data):
            batch = tuple(t.to(self.device) for t in batch)
            input_ids, input_mask, label_ids = batch

            with torch.no_grad():
                # print(input_mask)
                outputs = model(input_ids, input_mask, labels=None)
                logits, sequence_output = outputs
                # logits, probs = outputs

            # of last hidden state with size (batch_size, sequence_length, hidden_size)
            # where batch_size=1, sequence_length=95, hidden_size=768)
            # take average of sequence, size (batch_size, hidden_size)
            '''
            if emb_type == 'poolbert':
                emb_output = pooled_output
            elif emb_type == "avbert":
                emb_output = sequence_output.mean(axis=1)


            if self.use_cuda:
                emb_output = list(emb_output[0].detach().cpu().numpy())  # .detach().cpu() necessary here on gpu

            else:
                self.logger.info("NOT USING CUDA")
                emb_output = list(emb_output[0].numpy())
            embeddings.append(emb_output)
            '''
            logits = logits.detach().cpu().numpy()
            #probs = probs.detach().cpu().numpy()

            if output_mode == 'bio_classification':
                pred = [list(p) for p in np.argmax(logits, axis=2)]
            elif output_mode == 'classification':
                #print(probs)
                #assert len(probs[0]) == 2
                pred = np.argmax(logits, axis=1)
            preds.extend(pred)

        model.train()
        if return_embeddings:
            return embeddings
        else:
            return preds

    def eval(self, model, data, labels, av_loss=None, set_type='dev', name='Basil', output_mode='classification'):
        preds = self.predict(model, data, output_mode=output_mode)
        # print('Evaluation these predictions:', len(preds), len(preds[0]), preds[:2])
        # print('Evaluation above predictions with these labels:', len(labels), len(labels[0]), labels[:2])
        if output_mode == 'bio_classification':
            labels = labels.numpy().flatten()
            preds = np.asarray(preds)
            preds = np.reshape(preds, labels.shape)
        else:
            labels = labels.numpy()

        if len(preds) != len(labels):
            print('Sizes not equal')
            print(preds, labels)
            print(len(preds), len(preds[0]))
            print(len(labels), len(labels[0]))
            exit(0)

        metrics_dict, metrics_string = my_eval(labels, preds, set_type=set_type, av_loss=av_loss, name=name)

        # output_eval_file = os.path.join(self.reports_dir, f"{name}_eval_results.txt")
        # self.logger.info(f'{metrics_string}')
        # with open(output_eval_file, 'w') as f:
        #    f.write{metrics_string + '\n'}

        return metrics_dict, metrics_string


def save_model(model_to_save, model_dir, identifier):
    output_dir = os.path.join(model_dir, identifier)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    output_model_file = os.path.join(output_dir, "pytorch_model.bin")
    output_config_file = os.path.join(output_dir, "config.json")

    model_to_save = model_to_save.module if hasattr(model_to_save,
                                                    'module') else model_to_save  # Only save the model it-self
    torch.save(model_to_save.state_dict(), output_model_file)
    model_to_save.config.to_json_file(output_config_file)

    # test again