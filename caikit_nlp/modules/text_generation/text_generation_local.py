# Copyright The Caikit Authors
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


# Standard
from typing import Optional
import gc
import os

# Third Party
from datasets import Dataset
from datasets import IterableDataset as TransformersIterableDataset
from torch.utils.data import IterableDataset
from transformers import AutoConfig, AutoTokenizer
import torch

# First Party
from caikit import get_config
from caikit.core.data_model import DataStream
from caikit.core.modules import ModuleBase, ModuleConfig, ModuleSaver, module
from caikit.core.toolkit import error_handler
from caikit.interfaces.nlp.data_model import GeneratedTextResult
from caikit.interfaces.nlp.tasks import TextGenerationTask
import alog

# Local
from ...data_model import GenerationTrainRecord
from ...resources.pretrained_model import (
    HFAutoCausalLM,
    HFAutoSeq2SeqLM,
    PretrainedModelBase,
)
from ...toolkit.data_type_utils import get_torch_dtype, str_to_torch_dtype
from ...toolkit.text_generation.model_run_utils import (
    GENERATE_FUNCTION_ARGS,
    generate_text_func,
)
from ...toolkit.torch_run import get_torch_elastic_launch_config

log = alog.use_channel("TXT_GEN")
error = error_handler.get(log)


# pylint: disable=too-many-lines,too-many-instance-attributes
@module(
    id="f9181353-4ccf-4572-bd1e-f12bcda26792",
    name="Text Generation",
    version="0.1.0",
    task=TextGenerationTask,
)
class TextGeneration(ModuleBase):
    """Module to provide text generation capabilities"""

    RANDOM_SEED = 73
    supported_resources = [HFAutoCausalLM, HFAutoSeq2SeqLM]

    # Below list is taken from
    # https://huggingface.co/docs/transformers/main/en/main_classes/trainer#transformers.TrainingArguments
    allowed_training_args = {
        "weight_decay",
        "adam_beta1",
        "adam_beta2",
        "adam_epsilon",
        "max_grad_norm",
        "lr_scheduler_type",
        "warmup_ratio",
        "warmup_steps",
        "use_ipex",
        "disable_tqdm",
        "label_names",
        "optim",
        "optim_args",
        "group_by_length",
        "dataloader_pin_memory",
        "gradient_checkpointing",
        "full_determinism",
    }

    def __init__(
        self,
        model_name: str,
        model: PretrainedModelBase = None,
        bos_token: Optional[str] = None,
        sep_token: Optional[str] = None,
        eos_token: Optional[str] = None,
        pad_token: Optional[str] = None,
    ):
        super().__init__()

        error.type_check("<NLP48137045E>", str, allow_none=True, eos_token=eos_token)
        self.model = model
        self.model_name = model_name

        self._bos_token = bos_token
        self._sep_token = sep_token
        self._eos_token = eos_token
        self._pad_token = pad_token

    # pylint: disable=duplicate-code
    def __del__(self):
        del self.model
        gc.collect()
        try:
            torch.cuda.empty_cache()
        except AttributeError:
            pass

    @classmethod
    def bootstrap(cls, base_model_path: str, torch_dtype: str = "float32"):
        """Function to bootstrap a pre-trained transformers model and
        get a caikit text-generation 'model'.

        Args:
            base_model_path: str
                Path to transformers model
                NOTE: Model path needs to contain tokenizer as well
            torch_dtype: str
                Torch data type to be used when loading the model.
                Default: float32
        Returns:
            caikit_nlp.blocks.text_generation.TextGeneration
                Object of TextGeneration class (model)
        """
        # pylint: disable=duplicate-code
        model_config = AutoConfig.from_pretrained(base_model_path)

        resource_type = None
        for resource in cls.supported_resources:
            if model_config.model_type in resource.SUPPORTED_MODEL_TYPES:
                resource_type = resource
                break

        if not resource_type:
            error(
                "<NLP61784225E>",
                "{} model type is not supported currently!".format(
                    model_config.model_type
                ),
            )
        log.debug("Bootstrapping base resource [%s]", base_model_path)
        base_model = resource_type.bootstrap(
            base_model_path,
            tokenizer_name=base_model_path,
            torch_dtype=torch_dtype,
        )
        eos_token = base_model._tokenizer.eos_token or None
        return cls(
            base_model_path,
            base_model,
            eos_token=eos_token,
        )

    @classmethod
    def train(
        cls,
        base_model: str,  # TODO: Union[str, PretrainedModelBase]
        train_stream: DataStream[GenerationTrainRecord],
        torch_dtype: str = None,  # TODO: Optional[Union[torch.dtype, str]]
        max_source_length: int = 256,
        max_target_length: int = 128,
        batch_size: int = 8,
        num_epochs: int = 5,
        accumulate_steps: int = 32,
        random_seed: int = RANDOM_SEED,
        lr: float = 2e-5,
        # Directory where model predictions and checkpoints will be written
        checkpoint_dir: str = "/tmp/trained_model",
        use_iterable_dataset: bool = False,
        **kwargs,
    ) -> "TextGeneration":
        """
        Fine-tune a CausalLM or Seq2seq text generation model.

        Args:
            base_model:  Union[str, caikit_nlp.resources.pretrained_model.base.PretrainedModelBase]
                Base resource model used for underlying generation.
            train_stream: DataStream[GenerationTrainRecord] or DataStream[ClassificationTrainRecord]
                Data to be used for fine-tuning the generation model.
            torch_dtype: str
                TODO: Optional[Union[torch.dtype, str]]
                Data type to use for training/inference of the underlying text generation model.
                If no value is provided, we pull from torch_dtype in config. If an in memory
                resource is provided which does not match the specified data type, the model
                underpinning the resource will be converted in place to the correct torch dtype.
            max_source_length: int
                Max length of input sequences being considered. Default: 256.
            max_target_length: int
                Max length of target sequences being predicted. Default: 128.
            batch_size: int
                Batch sized to be used for training / evaluation data. Default: 8.
            num_epochs: int
                Number of epochs to tune the model. Default: 20.
            accumulate_steps: int
                Number of steps to use for gradient accumulation. Default: 1.
            lr: float
                Learning rate to be used while tuning model. Default: 2e-5.
            checkpoint_dir: str
                Directory where model predictions and checkpoints will be written
            use_iterable_dataset: bool
                Indicates that we should use an iterable dataset. If this is set to False, the
                whole dataset will be loaded into memory, but the sharded training will be faster.
                Default: False
            **kwargs:
                Arguments supported by HF Training Arguments.
                TrainingArguments:
                    https://huggingface.co/docs/transformers/v4.30.0/en/main_classes/trainer#transformers.TrainingArguments
                Seq2SeqTrainingArguments:
                    https://huggingface.co/docs/transformers/v4.30.0/en/main_classes/trainer#transformers.Seq2SeqTrainingArguments
        Returns:
            TextGeneration
                Instance of this class with fine-tuned models.
        """

        torch_dtype = get_torch_dtype(torch_dtype)

        # Make training deterministic
        if torch.cuda.is_available():
            torch.backends.cudnn.deterministic = True

        ## NOTE: Below code has been used in couple of places at this point, like in
        # text_generation module. In future, we would want to consolidate this into
        # a base class or a toolkit function
        # pylint: disable=duplicate-code
        resource_type = None

        ## Load base model
        if isinstance(base_model, str):
            model_config = AutoConfig.from_pretrained(base_model)

            for resource in cls.supported_resources:
                if model_config.model_type in resource.SUPPORTED_MODEL_TYPES:
                    resource_type = resource
                    break

            if not resource_type:
                error(
                    "<NLP61784225E>",
                    "{} model type is not supported currently!".format(
                        model_config.model_type
                    ),
                )
            log.debug("Bootstrapping base resource [%s]", base_model)
            base_model = resource_type.bootstrap(base_model, torch_dtype=torch_dtype)

        else:
            # base_model is actually a resource object
            resource_type = type(base_model)

        error.type_check("<NLP03221895E>", PretrainedModelBase, base_model=base_model)
        ## Generate data loader from stream
        training_dataset: IterableDataset = cls._preprocess_function(
            base_model=base_model,
            train_stream=train_stream,
            tokenizer=base_model.tokenizer,
            max_source_length=max_source_length,
            max_target_length=max_target_length,
            shuffle=True,
            use_iterable_dataset=use_iterable_dataset,
        )

        ### Dtype based processing
        # NOTE: Following is not exhaustive list of all parameters
        # for all dtypes
        if torch_dtype == torch.float16:
            dtype_based_params = {
                "fp16": True,
            }
        elif torch_dtype == torch.bfloat16:
            dtype_based_params = {
                "bf16": True,
            }
        else:
            # default to float32
            dtype_based_params = {}

        ## TODO: Add automatic sharding selection based on number of parameters
        # in base model
        ## TODO: Fetch trainer from resource

        # Filter **training_arguments to only process allowed ones
        filtered_training_arguments = {
            k: v for k, v in kwargs.items() if k in cls.allowed_training_args
        }

        extra_training_args = set(kwargs.keys()).difference(
            filtered_training_arguments.keys()
        )

        if extra_training_args:
            log.warning(
                "<NLP24424909W>",
                f"{extra_training_args} parameter(s) not allowed by {cls.__name__} currently and will be ignored!",
            )

        processing_configuration = {}

        # Conditionally enable sharding if multiple GPUs available
        if torch.cuda.is_available() and torch.cuda.device_count() > 1:
            processing_configuration = {
                "fsdp": "full_shard offload auto_wrap",
                "fsdp_config": {
                    # NOTE: Every transformers model has `_no_split_modules` property that can be
                    # leveraged to identify the layers to split. This seems to be a decent
                    # "default" behavior unless we want to optimize further. We will start with
                    # this generic approach, since it allows us to handle variety
                    # of models and iterate on it, based on what we encounter.
                    "fsdp_transformer_layer_cls_to_wrap": base_model._model._no_split_modules
                },
            }

        training_args = {
            "output_dir": checkpoint_dir,
            "per_device_train_batch_size": batch_size,
            "per_device_eval_batch_size": batch_size,
            "num_train_epochs": num_epochs,
            "seed": random_seed,
            # NOTE: We have disabled evaluation for now
            "do_eval": False,
            "do_train": True,
            "learning_rate": lr,
            "weight_decay": 0.01,
            "save_total_limit": 3,
            "push_to_hub": False,
            "no_cuda": False,  # Default
            "remove_unused_columns": True,
            "dataloader_pin_memory": False,
            "gradient_accumulation_steps": accumulate_steps,
            "eval_accumulation_steps": accumulate_steps,
            "gradient_checkpointing": True,
            "full_determinism": True,
            # Required for iterable dataset
            "max_steps": 50,
            # Some interesting parameters:
            "auto_find_batch_size": True,
            # NOTE: following can override above arguments in order
            **filtered_training_arguments,
            **processing_configuration,
            **dtype_based_params,
        }

        if num_epochs < 1:
            log.warning(
                "<NLP64076114W>",
                f"Number of epochs configured is {num_epochs} which is less than minimum 1. \
                    No training will be performed",
            )

            return TextGeneration(
                model_name=base_model._model_name,
                model=base_model,
            )

        launch_config = get_torch_elastic_launch_config(
            get_config().master_addr,
            get_config().master_port,
        )

        torch.distributed.launcher.api.elastic_launch(
            launch_config, cls._launch_training
        )(base_model, training_dataset, training_args, checkpoint_dir)

        # In case this program is started via torchrun, below might not work as is
        # because this case of multiple devices, this whole program gets run
        # in parallel, so the model might still be in "write" mode on 1 process
        # while we try to read it in below process.
        #
        # Note that we forward the base model tokenizer instance to bootstrap to avoid
        # dealing with temporarily exporting and reloading the tokenizer off of the trainer.
        model = resource_type.bootstrap(
            model_name=checkpoint_dir,
            tokenizer_name=base_model.tokenizer,
            torch_dtype=torch_dtype,
        )

        return cls(
            model_name=base_model._model_name,
            model=model,
            bos_token=model.tokenizer.bos_token or None,
            sep_token=model.tokenizer.sep_token or None,
            eos_token=model.tokenizer.eos_token or None,
            pad_token=model.tokenizer.pad_token or None,
        )

    @classmethod
    def load(
        cls,
        model_path: str,
        torch_dtype: str = None,
    ) -> "TextGeneration":
        """Function to load text-generation model

        Args:
            model_path: str
                Path to the model to be loaded.
            torch_dtype: str
                Torch data type to be used when loading the model.
        Returns:
            TextGeneration
                Instance of this class built from the on disk model.
        """

        config = ModuleConfig.load(model_path)

        if torch_dtype is not None:
            torch_dtype = str_to_torch_dtype(torch_dtype)
        elif config.trained_torch_dtype:
            torch_dtype = str_to_torch_dtype(config.trained_torch_dtype)

        base_model_path = config.get("artifact_path")
        error.type_check("<NLP35174683E>", str, base_model_path=base_model_path)

        base_model_path = os.path.join(model_path, base_model_path)
        error.dir_check("<NLP01983374E>", base_model_path)
        return cls.bootstrap(base_model_path, torch_dtype)

    def save(self, model_path):
        """Save caikit model

        Args:
            model_path: str
                Folder to save text-generation caikit model
        """
        saver = ModuleSaver(
            self,
            model_path=model_path,
        )
        with saver:
            artifacts_dir = "artifacts"
            saver.update_config(
                {
                    "artifact_path": artifacts_dir,
                    "eos_token": self._eos_token,
                    "torch_dtype": str(self.model._torch_dtype),
                }
            )
            if self.model:
                # This will save both tokenizer and base model
                self.model.save(
                    model_path,
                    tokenizer_dirname=artifacts_dir,
                    base_model_dirname=artifacts_dir,
                )

    def run(
        self,
        text: str,
        max_new_tokens: Optional[int] = 20,
        min_new_tokens: Optional[int] = 0,
        truncate_input_tokens: Optional[int] = 0,
        decoding_method: Optional[str] = "GREEDY",
        top_k: Optional[int] = 0,
        top_p: Optional[float] = 0.0,
        typical_p: Optional[float] = 0.0,
        temperature: Optional[float] = 1.0,
        repetition_penalty: Optional[float] = 0.0,
        max_time: Optional[float] = None,
        **kwargs,
    ) -> GeneratedTextResult:

        """
            Run the full text generation model.
            Args:
                {}
            Returns:
                GeneratedTextResult
                    Generated text result produced by the model.
        """.format(
            GENERATE_FUNCTION_ARGS
        )

        # TODO: Beam search currently not supported

        return generate_text_func(
            # Pass HF model
            self.model.model,
            self.model.tokenizer,
            self.PRODUCER_ID,
            self._eos_token,
            text,
            max_new_tokens=max_new_tokens,
            min_new_tokens=min_new_tokens,
            truncate_input_tokens=truncate_input_tokens,
            decoding_method=decoding_method,
            top_k=top_k,
            top_p=top_p,
            typical_p=typical_p,
            temperature=temperature,
            repetition_penalty=repetition_penalty,
            max_time=max_time,
            **kwargs,
        )

    ################################## Private Functions ######################################

    @staticmethod
    def _preprocess_function(
        base_model: PretrainedModelBase,
        train_stream: DataStream[GenerationTrainRecord],
        tokenizer: AutoTokenizer,
        max_source_length: int,
        max_target_length: int,
        shuffle: bool,
        use_iterable_dataset: bool,
    ):
        """Pre-process each example to get it prepared for training."""
        if use_iterable_dataset:
            # Generator based
            log.debug("Loading data as an iterable dataset")
            dataset = TransformersIterableDataset.from_generator(
                get, gen_kwargs={"train_stream": train_stream}
            )
        else:
            # Convert the train stream to an normal dataset in memory
            log.debug("Loading data as a normal dataset")
            # TODO: Optimize and clean this up!
            inputs = []
            outputs = []
            if base_model.REQUIRES_TOKEN_UNWRAPPING:
                for substream in train_stream:
                    for data in substream:
                        inputs.append(data.input)
                        outputs.append(data.output)
            else:
                for data in train_stream:
                    inputs.append(data.input)
                    outputs.append(data.output)
            dataset = Dataset.from_dict({"input": inputs, "output": outputs})
        # Map our HF datasets; with our tokenizer functions
        mapped_dataset = dataset.map(
            base_model.tokenize_function,
            fn_kwargs={
                "tokenizer": tokenizer,
                "max_source_length": max_source_length,
                "max_target_length": max_target_length,
            },
        )
        return mapped_dataset

    @staticmethod
    def _launch_training(
        base_model, training_dataset, training_args, checkpoint_dir
    ) -> None:
        """Utility function to wrap trainer and execute training"""

        trainer = base_model.get_trainer(
            train_dataset=training_dataset, **training_args
        )

        # Start training via Trainer.train function
        trainer.train()

        # save the model temporarily and reload it
        # this is done, since otherwise the model might be distributed in different
        # devices, in which case its better to use trainer's `prediction_step`
        # functions, but then, they don't always give API similar to `generate`
        # and thus cause incompatibilities in `run` function
        trainer.save_state()
        trainer.save_model(checkpoint_dir)


def get(train_stream):
    for data in train_stream:
        # Handle token unwrapping for causal language modeling
        if isinstance(data, DataStream):
            for datum in data:
               yield {"input": datum.input, "output": datum.output}
        # Otherwise assume we directly yield dictionaries
        else:
            yield {"input": data.input, "output": data.output}
