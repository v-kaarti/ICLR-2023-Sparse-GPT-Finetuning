# coding=utf-8
# Copyright 2021 The HuggingFace Inc. team. All rights reserved.
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
import argparse
import gc
import os

import torch
from datasets import load_dataset
from torch.utils.data import DataLoader
from transformers import  DataCollatorForLanguageModeling, OPTForCausalLM, AutoTokenizer, get_linear_schedule_with_warmup, set_seed
from utils.save_utils import load_masked_model, load_masked_model_single
from utils.prehook_utils import put_backward_hooks, remove_all_hooks, check_whitelist

from accelerate import Accelerator, DistributedType
from tqdm import tqdm

########################################################################
# This is a fully working simple example to use Accelerate
#
# This example trains a Bert base model on GLUE MRPC
# in any of the following settings (with the same script):
#   - single CPU or single GPU
#   - multi GPUS (using PyTorch distributed mode)
#   - (multi) TPUs
#   - fp16 (mixed-precision) or fp32 (normal precision)
#   - FSDP
#
# This example also demonstrates the checkpointing and sharding capabilities
#
# To run it in each of these various modes, follow the instructions
# in the readme for examples:
# https://github.com/huggingface/accelerate/tree/main/examples
#
########################################################################


MAX_GPU_BATCH_SIZE = 1


# New Code #
# Converting Bytes to Megabytes
def b2mb(x):
    return int(x / 2**20)


# New Code #
# This context manager is used to track the peak memory usage of the process
class TorchTracemalloc:
    def __enter__(self):
        gc.collect()
        torch.cuda.empty_cache()
        torch.cuda.reset_max_memory_allocated()  # reset the peak gauge to zero
        self.begin = torch.cuda.memory_allocated()
        return self

    def __exit__(self, *exc):
        gc.collect()
        torch.cuda.empty_cache()
        self.end = torch.cuda.memory_allocated()
        self.peak = torch.cuda.max_memory_allocated()
        self.used = b2mb(self.end - self.begin)
        self.peaked = b2mb(self.peak - self.begin)
        # print(f"delta used/peak {self.used:4d}/{self.peaked:4d}")


# For testing only
if os.environ.get("TESTING_MOCKED_DATALOADERS", None) == "1":
    from accelerate.test_utils.training import mocked_dataloaders

    get_dataloaders = mocked_dataloaders  # noqa: F811


def training_function(config, args):
    # For testing only
    if os.environ.get("TESTING_MOCKED_DATALOADERS", None) == "1":
        config["num_epochs"] = 2
    # Initialize accelerator
    if args.with_tracking:
        accelerator = Accelerator(
            cpu=args.cpu, mixed_precision=args.mixed_precision, log_with="wandb", logging_dir=args.logging_dir
        )
    else:
        accelerator = Accelerator()
    accelerator.print(accelerator.distributed_type)

    if hasattr(args.checkpointing_steps, "isdigit"):
        if args.checkpointing_steps == "epoch":
            checkpointing_steps = args.checkpointing_steps
        elif args.checkpointing_steps.isdigit():
            checkpointing_steps = int(args.checkpointing_steps)
        else:
            raise ValueError(
                f"Argument `checkpointing_steps` must be either a number or `epoch`. `{args.checkpointing_steps}` passed."
            )
    else:
        checkpointing_steps = None
    # Sample hyper-parameters for learning rate, batch size, seed and a few other HPs
    lr = config["lr"]
    num_epochs = int(config["num_epochs"])
    seed = int(config["seed"])
    batch_size = int(config["batch_size"])

    # We need to initialize the trackers we use, and also store our configuration
    if args.with_tracking:
        experiment_config = vars(args)
        accelerator.init_trackers("fsdp_glue_no_trainer", experiment_config)

    tokenizer = AutoTokenizer.from_pretrained(f'facebook/{config["model_name"]}', padding_side='left', model_max_length=512)
    #datasets = load_dataset("glue", "mrpc")
    datasets = load_dataset('wikitext', 'wikitext-2-raw-v1', streaming=True)

    def tokenize_function(examples):
        # max_length=None => use the model max length (it's actually the default)
        return tokenizer(examples['text'], truncation=True, padding='max_length')

    # Apply the method we just defined to all the examples in all the splits of the dataset
    # starting with the main process first:
    with accelerator.main_process_first():
        tokenized_datasets = datasets.map(
            tokenize_function,
            batched=True,
            remove_columns=["text"],
        )

    # We also rename the 'label' column to 'labels' which is the expected name for labels by the models of the
    # transformers library
    #tokenized_datasets = tokenized_datasets.rename_column("label", "labels")

    # If the batch size is too big we use gradient accumulation
    gradient_accumulation_steps = 1
    if batch_size > MAX_GPU_BATCH_SIZE and accelerator.distributed_type != DistributedType.TPU:
        gradient_accumulation_steps = batch_size // MAX_GPU_BATCH_SIZE
        batch_size = MAX_GPU_BATCH_SIZE

    def collate_fn(examples):
        # On TPU it's best to pad everything to the same length or training will be very slow.
        if accelerator.distributed_type == DistributedType.TPU:
            return tokenizer.pad(examples, padding="max_length", max_length=512, return_tensors="pt")
        return tokenizer.pad(examples, padding="longest", return_tensors="pt")

    # Instantiate dataloaders.
    train_dataloader = DataLoader(
        tokenized_datasets["train"].with_format("torch"), collate_fn=collate_fn, batch_size=batch_size
    )

    set_seed(seed)

    # Instantiate the model (we build the model here so that the seed also control new weights initialization)
    #model = AutoModelForSequenceClassification.from_pretrained(args.model_name_or_path, return_dict=True)
    if not config.get('model'):
        model = OPTForCausalLM.from_pretrained(f'facebook/{config["model_name"]}',
                                                          output_attentions=True,
                                                          output_hidden_states=True)

        load_masked_model_single(model, f'pruned_models/{config["model_name"]}-{config["sparsity"]}.pt')
    else:
        model = config.get('model')


    # back_hooks = put_backward_hooks(model=model)

    for name, param in model.named_parameters():
        if 'weight' in name and check_whitelist(name):
            mask = param != 0
            print(f"prop nonzeros: {torch.sum(mask) / torch.numel(param)}")
            def hook(grad, mask=mask):
                return grad * mask.float()
            param.register_hook(hook)

    # New Code #
    # For FSDP feature, it is highly recommended and efficient to prepare the model before creating optimizer
    model = accelerator.prepare(model)
    #accelerator.print(model)

    # Instantiate optimizer
    # New Code #
    # For FSDP feature, at present it doesn't support multiple parameter groups,
    # so we need to create a single parameter group for the whole model
    optimizer = torch.optim.AdamW(params=model.parameters(), lr=lr, weight_decay=2e-4)

    # Instantiate scheduler
    lr_scheduler = get_linear_schedule_with_warmup(
        optimizer=optimizer,
        num_warmup_steps=2,
        num_training_steps=(config["train_steps"] * num_epochs) // gradient_accumulation_steps,
    )

    # New Code #
    # For FSDP feature, prepare everything except the model as we have already prepared the model
    # before creating the optimizer
    # There is no specific order to remember, we just need to unpack the objects in the same order we gave them to the
    # prepare method.
    optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
        optimizer, train_dataloader, lr_scheduler
    )

    overall_step = 0

    # Potentially load in the weights and states from a previous save
    if args.resume_from_checkpoint:
        if args.resume_from_checkpoint is not None or args.resume_from_checkpoint != "":
            accelerator.print(f"Resumed from checkpoint: {args.resume_from_checkpoint}")
            accelerator.load_state(args.resume_from_checkpoint)
            path = os.path.basename(args.resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = [f.name for f in os.scandir(os.getcwd()) if f.is_dir()]
            dirs.sort(key=os.path.getctime)
            path = dirs[-1]  # Sorts folders by date modified, most recent checkpoint is the last
        # Extract `epoch_{i}` or `step_{i}`
        training_difference = os.path.splitext(path)[0]

        if "epoch" in training_difference:
            num_epochs -= int(training_difference.replace("epoch_", ""))
            resume_step = None
        else:
            resume_step = int(training_difference.replace("step_", ""))
            num_epochs -= resume_step // len(train_dataloader)
            # If resuming by step, we also need to know exactly how far into the DataLoader we went
            resume_step = (num_epochs * len(train_dataloader)) - resume_step

    # Now we train the model
    for epoch in tqdm(range(num_epochs)):
        # New Code #
        # context manager to track the peak memory usage during the training epoch
        with TorchTracemalloc() as tracemalloc:
            model.train()
            if args.with_tracking:
                total_loss = 0
            for step, batch in enumerate(train_dataloader):
                if step == config['max_step']:
                    break
                # We need to skip steps until we reach the resumed step
                if args.resume_from_checkpoint and epoch == 0:
                    if resume_step is not None and step < resume_step:
                        pass
                # We could avoid this line since we set the accelerator with `device_placement=True`.
                batch.to(accelerator.device)
                outputs = model(**batch, labels=batch['input_ids'])
                # print(f"max memory: {torch.cuda.memory_allocated()}")
                loss = outputs.loss
                #print(f'Loss: {loss}')
                loss = loss / gradient_accumulation_steps
                # We keep track of the loss at each epoch
                if args.with_tracking:
                    total_loss += loss.detach().float()
                accelerator.backward(loss)
                if step % gradient_accumulation_steps == 0:
                    optimizer.step()
                    lr_scheduler.step()
                    optimizer.zero_grad()
                    # accelerator.print(lr_scheduler.get_lr())

                overall_step += 1

                if isinstance(checkpointing_steps, int):
                    output_dir = f"step_{overall_step}"
                    if overall_step % checkpointing_steps == 0:
                        if args.output_dir is not None:
                            output_dir = os.path.join(args.output_dir, output_dir)
                        accelerator.save_state(output_dir)
        # New Code #
        # Printing the GPU memory usage details such as allocated memory, peak memory, and total memory usage
        accelerator.print("Memory before entering the train : {}".format(b2mb(tracemalloc.begin)))
        accelerator.print("Memory consumed at the end of the train (end-begin): {}".format(tracemalloc.used))
        accelerator.print("Peak Memory consumed during the train (max-begin): {}".format(tracemalloc.peaked))
        accelerator.print(
            "Total Peak Memory consumed during the train (max): {}".format(
                tracemalloc.peaked + b2mb(tracemalloc.begin)
            )
        )
        # Logging the peak memory usage of the GPU to the tracker
        if args.with_tracking:
            accelerator.log(
                {
                    "train_total_peak_memory": tracemalloc.peaked + b2mb(tracemalloc.begin),
                },
                step=epoch,
        )
    remove_all_hooks(model)
    torch.cuda.empty_cache()

    if not config.get('save_model') or config['save_model']:
        torch.save(model, f'pruned_models/{config["model_name"]}-{config["sparsity"]}-finetuned.pt')


def fsdp_finetune(config):
    parser = argparse.ArgumentParser(description="Simple example of training script.")
    parser.add_argument(
        "--mixed_precision",
        type=str,
        default=None,
        choices=["no", "fp16", "bf16"],
        help="Whether to use mixed precision. Choose"
        "between fp16 and bf16 (bfloat16). Bf16 requires PyTorch >= 1.10."
        "and an Nvidia Ampere GPU.",
    )
    parser.add_argument("--cpu", action="store_true", help="If passed, will train on the CPU.")
    parser.add_argument(
        "--checkpointing_steps",
        type=str,
        default=None,
        help="Whether the various states should be saved at the end of every n steps, or 'epoch' for each epoch.",
    )
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help="If the training should continue from a checkpoint folder.",
    )
    parser.add_argument(
        "--with_tracking",
        action="store_true",
        help="Whether to load in all available experiment trackers from the environment and use them for logging.",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default=".",
        help="Optional save directory where all checkpoint folders will be stored. Default is the current working directory.",
    )
    parser.add_argument(
        "--logging_dir",
        type=str,
        default="logs",
        help="Location on where to store experiment tracking logs`",
    )
    #args = parser.parse_args()
    args = parser.parse_args(['--mixed_precision', 'fp16'])
    training_function(config, args)