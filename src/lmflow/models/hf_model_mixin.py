#!/usr/bin/env python
# coding=utf-8
# Copyright 2024 Statistics and Machine Learning Research Group. All rights reserved.
import os
import logging
from typing import Union, Optional, Dict

import torch
import deepspeed
from transformers import (
    CONFIG_MAPPING,
    AutoConfig,
    BitsAndBytesConfig,
    AutoTokenizer,
    PreTrainedTokenizer,
    PreTrainedTokenizerFast,
    AutoModelForCausalLM,
    AutoModelForSequenceClassification,
)
from peft import (
    LoraConfig,
    PeftModel,
    TaskType,
    get_peft_model,
    prepare_model_for_kbit_training
)
from peft.utils.constants import TRANSFORMERS_MODELS_TO_LORA_TARGET_MODULES_MAPPING

from lmflow.models.base_model import BaseModel
from lmflow.utils.constants import (
    LMFLOW_LORA_TARGET_MODULES_MAPPING
)
from lmflow.args import ModelArguments


logger = logging.getLogger(__name__)


HF_AUTOMODEL_MAPPING = {
    "decoder_only": AutoModelForCausalLM,
    "text_regression": AutoModelForSequenceClassification
}

HF_AUTOMODEL_TYPE = Union[AutoModelForCausalLM, AutoModelForSequenceClassification]

LORA_TARGET_MODULES_MAPPING = {
    k: TRANSFORMERS_MODELS_TO_LORA_TARGET_MODULES_MAPPING.get(k, LMFLOW_LORA_TARGET_MODULES_MAPPING.get(k)) 
    for k in set(TRANSFORMERS_MODELS_TO_LORA_TARGET_MODULES_MAPPING) | set(LMFLOW_LORA_TARGET_MODULES_MAPPING)
}


class HFModelMixin(BaseModel):
    def __init__(
        self,
        model_args: ModelArguments,
        do_train: bool,
        ds_config=None,
        device: Optional[str]="gpu",
        use_accelerator: bool=False,
        hf_auto_model_additional_args: Optional[Dict]=None,
        *args,
        **kwargs
    ):
        """Initializes a HFModel instance.

        Parameters
        ----------
        model_args : 
            Dictionary with model arguments such as model name, path, revision, etc.
        do_train : bool
            To prepare the model for training or inference.
        ds_config : optional
            Deepspeed configuration for distributed training, by default None
        device : str, optional
            By default "gpu"
        use_accelerator : bool, optional
            By default False
        """

        # See more about loading any type of standard or custom dataset (from
        # files, python dict, pandas DataFrame, etc) at
        # https://huggingface.co/docs/datasets/loading_datasets.html.

        # Load pretrained model and tokenizer
        #
        # Distributed training: The .from_pretrained methods guarantee that
        # only one local process can concurrently download model & vocab.

        self.device = device
        self.model_args = model_args
        self.tokenizer = self.__prepare_tokenizer(model_args)
        self.torch_dtype = self.__prepare_dtype(model_args)
        self.hf_model_config = self.__prepare_model_config(model_args, hf_auto_model_additional_args)
        self.quant_config = self.__prepare_quant_config(model_args)
        self.peft_config = self.__prepare_peft_config(model_args)
        
        # Some implementations require custom modules to be injected into the model.
        self.__model_module_inject(model_args)

        hf_auto_model = HF_AUTOMODEL_MAPPING[model_args.arch_type]
        if do_train:
            self.__prepare_model_for_training(model_args, hf_auto_model)
        else:
            self.__prepare_model_for_inference(model_args, hf_auto_model, use_accelerator, ds_config)
            
        # some post processing
        if self.tokenizer.eos_token_id is None:
            self.tokenizer.eos_token_id = self.backend_model.config.eos_token_id
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token_id = self.tokenizer.eos_token_id
        if self.backend_model.config.pad_token_id is None:
            self.backend_model.config.pad_token_id = self.tokenizer.pad_token_id
            

    def __prepare_tokenizer(
        self,
        model_args: ModelArguments,
    ) -> Union[PreTrainedTokenizer, PreTrainedTokenizerFast]:
        tokenizer_kwargs = {
            "cache_dir": model_args.cache_dir,
            "use_fast": model_args.use_fast_tokenizer,
            "revision": model_args.model_revision,
            "use_auth_token": True if model_args.use_auth_token else None,
            "trust_remote_code": model_args.trust_remote_code,
        }
        if model_args.padding_side != 'auto':
            tokenizer_kwargs["padding_side"] = model_args.padding_side
        
        try:
            if model_args.tokenizer_name:
                tokenizer = AutoTokenizer.from_pretrained(model_args.tokenizer_name, **tokenizer_kwargs)
            elif model_args.model_name_or_path:
                tokenizer = AutoTokenizer.from_pretrained(model_args.model_name_or_path, **tokenizer_kwargs)
            else:
                raise ValueError(
                    "You are instantiating a new tokenizer from scratch. This is"
                    " not supported by this script. You can do it from another"
                    " script, save it, and load it from here, using"
                    " --tokenizer_name."
                )

        except RecursionError:
            logger.warning(
                "The tokenizer_config.json file doesn't set the special tokens. Using default values: "
                "<unk>, <s>, </s> for unknown token, bos token and eos token respectively.")
            if model_args.tokenizer_name:
                tokenizer = AutoTokenizer.from_pretrained(model_args.tokenizer_name, unk_token="<unk>",
                                                    bos_token="<s>",
                                                    eos_token="</s>",
                                                    **tokenizer_kwargs)
            elif model_args.model_name_or_path:
                tokenizer = AutoTokenizer.from_pretrained(model_args.model_name_or_path, unk_token="<unk>",
                                                    bos_token="<s>",
                                                    eos_token="</s>",
                                                    **tokenizer_kwargs)
            else:
                raise ValueError(
                    "You are instantiating a new tokenizer from scratch. This is"
                    " not supported by this script. You can do it from another"
                    " script, save it, and load it from here, using"
                    " --tokenizer_name."
                )
            
        tokenizer.truncation_side = model_args.truncation_side or tokenizer.truncation_side
        tokenizer.model_max_length = model_args.model_max_length or tokenizer.model_max_length
        
        return tokenizer
    
    
    def __prepare_dtype(
        self,
        model_args: ModelArguments,
    ) -> torch.dtype:
        if model_args.arch_type == 'text_regression':
            if model_args.torch_dtype in ["auto", None, "bf16", "bfloat16"]:
                torch_dtype = torch.bfloat16
            else:
                torch_dtype = getattr(torch, model_args.torch_dtype)
                logger.warning(
                    f"If you are doing reward modeling,"
                    f" InstructGPT uses torch.bfloat16 for reward model, but you"
                    f" are using {torch_dtype} for your reward model init. Ignore"
                    f" this warning if it is intended.")
        else:
            torch_dtype = (
                model_args.torch_dtype
                if model_args.torch_dtype in ["auto", None]
                else getattr(torch, model_args.torch_dtype)
            )
            
        logger.debug(f"torch_dtype on init: {torch_dtype}")
        
        return torch_dtype


    def __prepare_model_config(
        self,
        model_args: ModelArguments,
        hf_auto_model_additional_args: Optional[Dict]=None,
    ):
        """Prepare model configuration for hf auto register,
        Parameters
        ----------
        model_args : ModelArguments
            LMFlow model arguments.
        hf_auto_model_additional_args : Optional[Dict], optional
            Special configurations such as `num_labels` in `AutoModelForSequenceClassification` 
            (commonly used in reward modeling) will not preset in __prepare_model_config, 
            so it should be passed in hf_auto_model_additional_args.
        Returns
        -------
        config : ModelConfig
            hf model config.
        """
        config_kwargs = {
            "torch_dtype": self.torch_dtype,
            "attn_implementation": "flash_attention_2" if model_args.use_flash_attention else None,
            "cache_dir": model_args.cache_dir,
            "revision": model_args.model_revision,
            "use_auth_token": True if model_args.use_auth_token else None,
            "trust_remote_code": model_args.trust_remote_code,
            "from_tf": bool(".ckpt" in model_args.model_name_or_path),
        }
        if hf_auto_model_additional_args is not None:
            config_kwargs.update(hf_auto_model_additional_args)
            
        if model_args.config_name:
            config = AutoConfig.from_pretrained(model_args.config_name, **config_kwargs)
        elif model_args.model_name_or_path:
            config = AutoConfig.from_pretrained(model_args.model_name_or_path, **config_kwargs)
        else:
            config = CONFIG_MAPPING[model_args.model_type]()
            logger.warning("You are instantiating a new config instance from scratch.")
            if model_args.config_overrides is not None:
                logger.info(f"Overriding config: {model_args.config_overrides}")
                config.update_from_string(model_args.config_overrides)
                logger.info(f"New config: {config}")
        
        return config
    
    
    def __prepare_quant_config(
        self,
        model_args: ModelArguments,
    ):
        quant_config = None
        if model_args.use_qlora:
            quant_config = BitsAndBytesConfig(
                load_in_4bit=model_args.bits == 4,
                load_in_8bit=model_args.bits == 8,
                llm_int8_threshold=6.0,
                llm_int8_has_fp16_weight=False,
                bnb_4bit_compute_dtype=self.torch_dtype,
                bnb_4bit_use_double_quant=model_args.double_quant,
                bnb_4bit_quant_type=model_args.quant_type,
            )
        
        return quant_config
    
    
    def __prepare_peft_config(
        self,
        model_args: ModelArguments,
    ):
        peft_config = None
        if model_args.use_lora:
            if model_args.lora_target_modules:
                lora_target_modules = model_args.lora_target_modules
            else:
                model_config = self.hf_model_config
                if hasattr(model_config, "to_dict"):
                    model_config = model_config.to_dict()
                if "model_type" not in model_config or not model_config["model_type"]:
                    logger.warning("It seems that your base model is a custom model, since "
                                   "model_type is not found in model_config when preparing peft config. "
                                   "Setting model_type to 'custom' as a fallback.")
                    model_config["model_type"] = "custom"
                lora_target_modules = LORA_TARGET_MODULES_MAPPING.get(model_config["model_type"], None)

            peft_config = LoraConfig(
                task_type=TaskType.CAUSAL_LM,
                inference_mode=False,
                r=model_args.lora_r,
                lora_alpha=model_args.lora_alpha,
                lora_dropout=model_args.lora_dropout,
                target_modules=lora_target_modules,
            )
            
        return peft_config
    
    
    def __model_module_inject(
        self,
        model_args: ModelArguments,
    ) -> None:
        """Override some model modules with custom implementations.
        
        Current implementations:
        - Position interpolation (model_args.do_rope_scaling): 
            replace llama embeddings with condense embeddings.
        """
        # position interpolation
        if model_args.do_rope_scaling:
            if "LlamaForCausalLM" in self.model_config.architectures:
                from lmflow.utils.position_interpolation.llama_rope_scaled_monkey_patch import (
                        replace_llama_with_condense,
                )
                replace_llama_with_condense(model_args.rope_pi_ratio, model_args.rope_ntk_ratio)
                
                
    def __prepare_model_for_training(
        self,
        model_args: ModelArguments,
        hf_auto_model: HF_AUTOMODEL_TYPE,
    ):
        # TODO: change to accelerate
        logger.info("Preparing model for training")
        if model_args.model_name_or_path:
            model = hf_auto_model.from_pretrained(
                model_args.model_name_or_path,
                config=self.hf_model_config,
                quantization_config=self.quant_config,
            )

            if model_args.use_qlora:
                model.gradient_checkpointing_enable()
                model = prepare_model_for_kbit_training(model)
        else:
            model = hf_auto_model.from_config(self.hf_model_config)
            n_params = sum(dict((p.data_ptr(), p.numel()) for p in model.parameters()).values())
            logger.info(f"Training new model from scratch - Total size={n_params/2**20:.2f}M params")
        self.backend_model_full = model
        
        if model_args.use_lora:
            model.enable_input_require_grads()
            model = get_peft_model(model, self.peft_config)
            model.print_trainable_parameters()

        # We resize the embeddings only when necessary to avoid index errors.
        # If you are creating a model from scratch on a small vocab and want a
        # smaller embedding size, remove this test.
        with deepspeed.zero.GatheredParameters(model.get_input_embeddings().weight, modifier_rank=None):
            weights = model.get_input_embeddings().weight
            embedding_size = weights.shape[0]
        if len(self.tokenizer) > embedding_size:
            model.resize_token_embeddings(len(self.tokenizer))

        self.backend_model = model

    
    def __prepare_model_for_inference(
        self,
        model_args: ModelArguments,
        hf_auto_model: HF_AUTOMODEL_TYPE,
        use_accelerator,
        ds_config
    ):
        # TODO: change to accelerate
        logger.info("Preparing model for inference")
        if use_accelerator:
            peft_model_id = model_args.lora_model_path
            self.backend_model = hf_auto_model.from_pretrained(
                    model_args.model_name_or_path,
                    config=self.hf_model_config,
                    device_map="auto",
                    offload_folder="offload",
                    offload_state_dict=True,
                    load_in_8bit = model_args.use_int8,
                )
            if peft_model_id is not None:
                self.backend_model = PeftModel.from_pretrained(
                    self.backend_model, 
                    peft_model_id,
                )
        else:
            from transformers.integrations import HfDeepSpeedConfig
            dschf = HfDeepSpeedConfig(ds_config)
            peft_model_id = model_args.lora_model_path
            # NOTE: Currently offload is not supported by llama
            if self.hf_model_config.model_type == "llama" and model_args.use_ram_optimized_load:
                logger.warning(
                    "llama does not support RAM optimized load. Automatically"
                    " use original load instead."
                )
                model_args.use_ram_optimized_load = False

            if model_args.use_ram_optimized_load and peft_model_id is None:
                try:
                    # RAM-optimized load
                    self.backend_model = hf_auto_model.from_pretrained(
                        model_args.model_name_or_path,
                        config=self.hf_model_config,
                        device_map="auto",
                        offload_folder="offload",
                        offload_state_dict=True,
                    )
                except:
                    logger.warning(
                        "Failed to use RAM optimized load. Automatically"
                        " use original load instead."
                    )
                    # Normal load
                    self.backend_model = hf_auto_model.from_pretrained(
                        model_args.model_name_or_path,
                        config=self.hf_model_config,
                    )
            else:
                if peft_model_id is not None:
                    logger.warning(
                        "LoRA does not support RAM optimized load currently."
                        " Automatically use original load instead."
                    )
                self.backend_model = hf_auto_model.from_pretrained(
                    model_args.model_name_or_path,
                    config=self.hf_model_config,
                )

            self.backend_model_full = self.backend_model
            if peft_model_id is not None:
                self.backend_model = PeftModel.from_pretrained(
                    self.backend_model, peft_model_id
                )

            if self.device == "gpu":
                deepspeed.init_distributed()
                self.ds_engine = deepspeed.initialize(model=self.backend_model, config_params=ds_config)[0]
                self.ds_engine.module.eval()
                
        self.tokenizer.padding_side = "left" # necessary for llama, gpt2 and other decoder models

    
    def get_max_length(self):
        """
        Return max acceptable input length in terms of tokens.
        """
        return self.tokenizer.model_max_length


    def get_tokenizer(self):
        """
        Return the tokenizer of the model.
        """
        return self.tokenizer


    def get_backend_model(self):
        """
        Return the backend model.
        """
        return self.backend_model
