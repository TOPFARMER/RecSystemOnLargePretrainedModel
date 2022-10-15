import torch
import torch.nn as nn
from transformers import OPTModel
from utils.pylogger import get_pylogger
from utils.schedule_functions import get_lr_scheduler_function
from models.layers import PromptEncoder, DeepPromptEncoder
from models.abstract_recommender import TextSeqRec
from models.configs import OPTSeqRecConfig, OPTPromptSeqRecConfig
from models.utils import mean_pooling, last_pooling

log = get_pylogger(__name__)


class OPTSeqRec(TextSeqRec):

    def __init__(self, config: OPTSeqRecConfig):
        self.save_hyperparameters()
        super().__init__(self.hparams.config)

        # parameters initialization
        self.apply(self._init_weights)

    def _set_plm_model(self, plm_name):
        self.opt = OPTModel.from_pretrained(plm_name)

    def _get_item_emb_dim(self):
        return self.opt.config.hidden_size

    def _freeze_plm_layers(self, num_unfreeze_layers):
        plm_n_layers = self.opt.config.num_hidden_layers
        if num_unfreeze_layers < -1 or num_unfreeze_layers > plm_n_layers:
            raise ValueError(
                f"num_unfreeze_layers {num_unfreeze_layers} is not supported.")

        if num_unfreeze_layers == -1:
            for param in self.opt.parameters():
                param.requires_grad = True
        else:
            for param in self.opt.parameters():
                param.requires_grad = False

        if num_unfreeze_layers > 0:
            unfreeze_layers = self.opt.decoder.layers[-num_unfreeze_layers:]
            for param in unfreeze_layers.parameters():
                param.requires_grad = True

    def _feature_extract(self, item_id_seq, item_seq_mask, tokenized_ids,
                         attention_mask):
        tokenized_ids = tokenized_ids.view(-1, tokenized_ids.shape[-1])
        attention_mask = attention_mask.view(-1, attention_mask.shape[-1])
        item_embs = self._get_opt_output(tokenized_ids, attention_mask)

        for layer in self.projection:
            item_embs = layer(item_embs)

        sasrec_seq_len = self.hparams.config.sasrec_seq_len
        sasrec_hidden_size = self.hparams.config.sasrec_hidden_size
        item_embs = item_embs.view(-1, sasrec_seq_len, sasrec_hidden_size)
        return item_embs

    def _get_opt_output(self, tokenized_ids, attention_mask):
        if self.hparams.config.plm_n_unfreeze_layers == 0:
            with torch.no_grad():
                output = self.opt(input_ids=tokenized_ids,
                                  attention_mask=attention_mask)
        else:
            output = self.opt(input_ids=tokenized_ids,
                              attention_mask=attention_mask)
        # (B * L_sas, L_plm, H_plm)
        sentence_embs = output.last_hidden_state
        pooling_method = self.hparams.config.pooling_method
        if pooling_method == "mean":
            # (B * L_sas, H_plm)
            item_embs = mean_pooling(sentence_embs,
                                     attention_mask).type_as(sentence_embs)
        elif pooling_method == "last":
            # (B * L_sas, H_plm)
            item_embs = last_pooling(sentence_embs,
                                     attention_mask).type_as(sentence_embs)
        return item_embs

    def _set_opt_lr(self, lr, decay, wd):
        tuning_params = []
        n_layers = self.opt.config.num_hidden_layers
        lrs = [lr * (decay**(n_layers - i)) for i in range(n_layers)]
        no_weight_decay = ["bias", "LayerNorm.weight"]

        for name, params in self.opt.named_parameters():
            if name.startswith("decoder.layers"):
                layer_idx = int(name.split(".")[2])
                p = {"params": params, "lr": lrs[layer_idx], "name": name}
            elif name.startswith("decoder.embed_"):
                p = {"params": params, "lr": lrs[0], "name": name}
            else:
                p = {"params": params, "lr": lrs[-1], "name": name}
            if any(nd in name for nd in no_weight_decay):
                p.update(weight_decay=0.0)
            else:
                p.update(weight_decay=wd)
            tuning_params.append(p)

        tuning_params = [
            layer for layer in tuning_params if layer["params"].requires_grad
        ]
        return tuning_params

    def configure_optimizers(self):
        lr = self.hparams.config.lr
        wd = self.hparams.config.weight_decay
        if self.hparams.config.plm_n_unfreeze_layers == 0:
            optimizer = torch.optim.AdamW(self.parameters(),
                                          lr=lr,
                                          weight_decay=wd)
        else:
            plm_lr = self.hparams.config.plm_lr
            layer_decay = self.hparams.config.plm_lr_layer_decay
            # set different learning rate for different layers
            opt_tuning_params = self._set_opt_lr(plm_lr, layer_decay, wd)
            opt_tuning_names = [
                "opt." + layer["name"] for layer in opt_tuning_params
            ]
            the_rest_params = []
            for name, params in self.named_parameters():
                if name not in opt_tuning_names:
                    the_rest_params.append(params)
            the_rest_params = [{
                "params": the_rest_params,
                "lr": lr,
                "weight_decay": wd,
                "name": "the_rest"
            }]
            all_params = opt_tuning_params + the_rest_params
            optimizer = torch.optim.AdamW(all_params)
        return optimizer

    @classmethod
    def add_model_specific_args(cls, parent_parser):
        parser = super(OPTSeqRec, cls).add_model_specific_args(parent_parser)
        parser = parent_parser.add_argument_group("OPTSeqRec")
        parser.add_argument("--plm_n_unfreeze_layers", type=int, default=0)
        # shared parameters of fine-tuneing PLM
        parser.add_argument("--plm_lr", type=float, default=1e-5)
        parser.add_argument("--plm_lr_layer_decay", type=float, default=0.8)
        parser.add_argument("--pooling_method", type=str, default="mean")
        return parent_parser

    @classmethod
    def build_model_config(cls, args, item_token_num):
        config = OPTSeqRecConfig(
            item_token_num=item_token_num,
            plm_n_unfreeze_layers=args.plm_n_unfreeze_layers,
            plm_lr=args.plm_lr,
            plm_lr_layer_decay=args.plm_lr_layer_decay,
            projection_n_layers=args.projection_n_layers,
            projection_inner_sizes=args.projection_inner_sizes,
            pooling_method=args.pooling_method,
        )
        config = super(OPTSeqRec, cls).build_model_config(args, config)
        return config


class OPTPromptSeqRec(OPTSeqRec):

    def __init__(self, config: OPTPromptSeqRecConfig):
        self.save_hyperparameters()
        super(OPTSeqRec, self).__init__(self.hparams.config)

        if config.pre_seq_len > 0:
            self.prefix_encoder = DeepPromptEncoder(
                plm=self.opt,
                prompt_projection=config.prompt_projection,
                prompt_seq_len=config.pre_seq_len,
                prompt_hidden_size=config.prompt_hidden_size,
                layer_norm_eps=config.layer_norm_eps)

        if config.post_seq_len > 0:
            self.postfix_encoder = DeepPromptEncoder(
                plm=self.opt,
                prompt_projection=config.prompt_projection,
                prompt_seq_len=config.post_seq_len,
                prompt_hidden_size=config.prompt_hidden_size,
                layer_norm_eps=config.layer_norm_eps)
            assert config.last_query_len >= 1, \
                "last_query_len must be at least 1"

        if config.last_query_len > 0:
            self.last_query_encoder = PromptEncoder(
                plm=self.opt, prompt_seq_len=config.last_query_len)

        if config.pooling_method == "mean_last":
            plm_hidden_size = self.opt.config.hidden_size
            eps = config.layer_norm_eps
            self.fusion_mlp = torch.nn.Sequential(
                torch.nn.Linear(plm_hidden_size * 2, plm_hidden_size),
                torch.nn.GELU(),
                torch.nn.Linear(plm_hidden_size, plm_hidden_size),
                torch.nn.GELU(),
                torch.nn.Linear(plm_hidden_size, plm_hidden_size),
                torch.nn.LayerNorm(plm_hidden_size, eps=eps))

        # parameters initialization
        self.apply(self._init_weights)

    def _get_opt_output(self, tokenized_ids, attention_mask):
        pre_seq_len = self.hparams.config.pre_seq_len
        post_seq_len = self.hparams.config.post_seq_len
        last_query_len = self.hparams.config.last_query_len
        plm_batch_size = tokenized_ids.shape[0]

        if pre_seq_len > 0:
            past_key_values = self.prefix_encoder(plm_batch_size)
            prefix_attention_mask = torch.ones(
                plm_batch_size, pre_seq_len).type_as(attention_mask)
            prompt_attention_mask = torch.cat(
                (prefix_attention_mask, attention_mask), dim=1)
            output = self.opt(
                input_ids=tokenized_ids,
                attention_mask=prompt_attention_mask,
                past_key_values=past_key_values,
            )
        else:
            output = self.opt(input_ids=tokenized_ids,
                              attention_mask=attention_mask)
            prompt_attention_mask = attention_mask
        past_key_values = output.past_key_values

        if last_query_len > 0:
            if post_seq_len > 0:
                prompt_key_values = self.postfix_encoder(plm_batch_size)
                new_past_key_values = []
                for past_key_value, prompt_key_value in zip(
                        past_key_values, prompt_key_values):
                    key_states = torch.cat(
                        (past_key_value[0], prompt_key_value[0]), dim=2)
                    values_states = torch.cat(
                        (past_key_value[1], prompt_key_value[1]), dim=2)
                    new_past_key_values.append((key_states, values_states))
                past_key_values = new_past_key_values

            post_fix_attention_mask = torch.ones(
                plm_batch_size,
                post_seq_len + last_query_len).type_as(attention_mask)
            prompt_attention_mask = torch.cat(
                (prompt_attention_mask, post_fix_attention_mask), dim=1)

            last_query_embs = self.last_query_encoder(plm_batch_size)
            last_token_embs = self.opt(
                inputs_embeds=last_query_embs,
                attention_mask=prompt_attention_mask,
                past_key_values=past_key_values,
            )

        sentence_embs = output.last_hidden_state  # (B * L_sas, L_plm, H_plm)
        pooling_method = self.hparams.config.pooling_method
        if pooling_method == "mean":
            # (B * L_sas, H_plm)
            item_embs = mean_pooling(sentence_embs, attention_mask)
        elif pooling_method == "last":
            # (B * L_sas, H_plm)
            item_embs = last_token_embs.last_hidden_state[:, -1, :]
        elif pooling_method == "mean_last":
            mean_embs = mean_pooling(sentence_embs, attention_mask)
            last_embs = last_token_embs.last_hidden_state[:, -1, :]
            item_embs = torch.cat([mean_embs, last_embs], dim=-1)
            # (B * L_sas, H_plm)
            item_embs = self.fusion_mlp(item_embs)
        return item_embs

    @classmethod
    def add_model_specific_args(cls, parent_parser):
        parser = super(OPTPromptSeqRec,
                       cls).add_model_specific_args(parent_parser)
        parser = parser.add_argument_group("OPTPromptSeqRec")
        parser.add_argument("--pre_seq_len", type=int, default=20)
        parser.add_argument("--post_seq_len", type=int, default=10)
        parser.add_argument("--last_query_len", type=int, default=1)
        parser.add_argument("--prompt_hidden_size", type=int, default=128)
        parser.add_argument("--prompt_projeciton",
                            type=str,
                            default="nonlinear")
        return parent_parser

    @classmethod
    def build_model_config(cls, args, item_token_num):
        config = OPTPromptSeqRecConfig(
            item_token_num=item_token_num,
            plm_n_unfreeze_layers=args.plm_n_unfreeze_layers,
            plm_lr=args.plm_lr,
            plm_lr_layer_decay=args.plm_lr_layer_decay,
            projection_n_layers=args.projection_n_layers,
            projection_inner_sizes=args.projection_inner_sizes,
            pooling_method=args.pooling_method,
            prompt_projection=args.prompt_projeciton,
            prompt_hidden_size=args.prompt_hidden_size,
            pre_seq_len=args.pre_seq_len,
            post_seq_len=args.post_seq_len,
            last_query_len=args.last_query_len,
        )
        config = super(OPTSeqRec, cls).build_model_config(args, config)
        return config


class PreInferOPTSeqRec(OPTSeqRec):

    def __init__(self, trainable_opt_part, tokenized_embs_lookup,
                 config: OPTSeqRecConfig):
        self.opt = trainable_opt_part
        self.tokenized_embs_lookup = tokenized_embs_lookup

        # normalized the tokenized_embs_lookup
        self.tokenized_embs_lookup = self.tokenized_embs_lookup / torch.norm(
            self.tokenized_embs_lookup, dim=1, keepdim=True)

        # transform the tokenized_embs_lookup to an embedding layer
        self.tokenized_embs_lookup = nn.Embedding.from_pretrained(
            self.tokenized_embs_lookup)
        self.tokenized_embs_lookup.weight.requires_grad = False

        self.save_hyperparameters(
            ignore=["trainable_opt_part", "tokenized_embs_lookup"])
        super(PreInferOPTSeqRec, self).__init__(self.hparams.config)

    def _set_plm_model(self, plm_name):
        pass

    def _freeze_plm_layers(self, num_unfreeze_layers):
        pass

    def _get_opt_output(self, tokenized_ids, attention_mask):
        if self.hparams.config.plm_n_unfreeze_layers == 0:
            tokenized_embs = self.tokenized_embs_lookup(tokenized_ids)
        else:
            tokenized_embs = self.tokenized_embs_lookup(tokenized_ids)
            output = self.opt(input_embs=tokenized_embs,
                              attention_mask=attention_mask)

        # (B * L_sas, L_plm, H_plm)
        sentence_embs = output.last_hidden_state
        pooling_method = self.hparams.config.pooling_method
        if pooling_method == "mean":
            # (B * L_sas, H_plm)
            item_embs = mean_pooling(sentence_embs,
                                     attention_mask).type_as(sentence_embs)
        elif pooling_method == "last":
            # (B * L_sas, H_plm)
            item_embs = last_pooling(sentence_embs,
                                     attention_mask).type_as(sentence_embs)
        return item_embs