"""
This script is used for training and test
"""


# from data_utils import convert_examples_to_relation_extraction_features
from data_utils import (features2tensors, relation_extraction_data_loader,
                        batch_to_model_input, RelationDataFormatSepProcessor,
                        RelationDataFormatUniProcessor)
from utils import acc_and_f1
from data_processing.io_utils import pkl_save, pkl_load, save_json
from transformers import glue_convert_examples_to_features as convert_examples_to_relation_extraction_features
from transformers import get_linear_schedule_with_warmup, get_cosine_schedule_with_warmup
from peft import LoraConfig, TaskType, get_peft_model, PeftModel, PeftConfig
import torch
from tqdm import trange, tqdm
import numpy as np
from packaging import version
from pathlib import Path
from config import SPEC_TAGS, MODEL_DICT, VERSION, NEW_ARGS, CONFIG_VERSION_NAME
import shutil
import os
import wandb
import pickle
from accelerate import Accelerator
# from transformers.deepspeed import HfDeepSpeedConfig
#import deepspeed

os.environ["WANDB_API_KEY"]=""
os.environ["WANDB_ENTITY"]="lathashree01"
os.environ["WANDB_PROJECT"]="final_ft_pretrain_llama2"


# hard coded params
mycache_dir="/vol/bitbucket/l22/llama2"
loss_filename="/final_loss_dict.pickle"

# Pretrained LLAMA 1
LLAMA1_PEFT_MODEL_PATH='/vol/bitbucket/l22/llama1_pretrained/pt_lora_model'

# Pretrained LLAMA 2
LLAMA2_PEFT_MODEL_PATH='/vol/bitbucket/l22/llama2_pretrained/pt_lora_model'


lora_trainable="q_proj,v_proj,k_proj"
modules_to_save="embed_tokens,lm_head"
lora_dropout=0.05
target_modules = lora_trainable.split(',')
mod_to_save = modules_to_save.split(',')
loss_dict={}

class TaskRunner(object):

    def __init__(self, args):
        super().__init__()

        self.args = args
        self.model_dict = MODEL_DICT
        self.train_data_loader = None
        self.dev_data_loader = None
        self.test_data_loader = None
        self.data_processor = None
        self.new_model_dir_path = Path(self.args.new_model_dir)
        self.new_model_dir_path.mkdir(parents=True, exist_ok=True)
        self._use_amp_for_fp16_from = 0
        self.loss_file_path = Path(self.args.new_model_dir+loss_filename)

    def task_runner_default_init(self):
        # set up data processor
        if self.data_processor is None:
            if self.args.data_format_mode == 0:
                self.data_processor = RelationDataFormatSepProcessor(
                    max_seq_len=self.args.max_seq_length, num_core=self.args.num_core)
            elif self.args.data_format_mode == 1:
                self.data_processor = RelationDataFormatUniProcessor(
                    max_seq_len=self.args.max_seq_length, num_core=self.args.num_core)
            else:
                raise NotImplementedError("Only support 0, 1 but get data_format_mode as {}"
                                          .format(self.args.data_format_mode))
        else:
            self.args.logger.warning("Use user defined data processor: {}".format(self.data_processor))

        self.data_processor.set_data_dir(self.args.data_dir)
        self.data_processor.set_header(self.args.data_file_header)

        # init or reload model
        if self.args.do_train:
            # init amp for fp16 (mix precision training)
            # _use_amp_for_fp16_from: 0 for no fp16; 1 for naive PyTorch amp; 2 for apex amp
            if self.args.fp16:
                self._load_amp_for_fp16()
            self._init_new_model()
        else:
            self._init_trained_model()

        # load data
        self.data_processor.set_tokenizer(self.tokenizer)
        self.data_processor.set_tokenizer_type(self.args.model_type)
        self.args.logger.info("data loader info: {}".format(self.data_processor))
        self._init_dataloader()

        if self.args.do_train:
            self._init_optimizer()

        self.args.logger.info("Model Config:\n{}".format(self.config))
        self.args.logger.info("All parameters:\n{}".format(self.args))

    def train(self):
        # create data loader
        self.args.logger.info("start training...")
        self.args.logger.info("Saving loss in file..{}".format(self.loss_file_path))
        tr_loss = .0
        t_step = 1
        latest_best_score = .0
        accelerator = Accelerator()
        self.train_data_loader, self.model, self.optimizer = accelerator.prepare(
                            self.train_data_loader, self.model, self.optimizer
                        )
        epoch_iter = trange(self.args.num_train_epochs, desc="Epoch", disable=not self.args.progress_bar)
        wandb.init()
        for epoch in epoch_iter:
            batch_iter = tqdm(self.train_data_loader, desc="Batch", disable=not self.args.progress_bar)
            batch_total_step = len(self.train_data_loader)
            for step, batch in enumerate(batch_iter):
                self.model.train()
                self.model.zero_grad()
                batch_input = batch_to_model_input(batch, model_type=self.args.model_type, device=self.args.device)

                if self.args.fp16 and self._use_amp_for_fp16_from == 1:
                    with self.amp.autocast():
                        batch_output = self.model(**batch_input)
                        loss = batch_output[0]
                else:
                    batch_output = self.model(**batch_input)
                    loss = batch_output[0]

                loss = loss / self.args.gradient_accumulation_steps
                tr_loss += loss.item()

                if self.args.fp16:
                    if self._use_amp_for_fp16_from == 1:
                        self.amp_scaler.scale(loss).backward()
                    elif self._use_amp_for_fp16_from == 2:
                        with self.amp.scale_loss(loss, self.optimizer) as scaled_loss:
                            scaled_loss.backward()
                else:
                    accelerator.backward(loss)
#                     loss.backward()

                # update gradient
                if (step + 1) % self.args.gradient_accumulation_steps == 0 or (step + 1) == batch_total_step:
                    if self.args.fp16:
                        if self._use_amp_for_fp16_from == 1:
                            self.amp_scaler.unscale_(self.optimizer)
                            torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.args.max_grad_norm)
                            self.amp_scaler.step(self.optimizer)
                            self.amp_scaler.update()
                        elif self._use_amp_for_fp16_from == 2:
                            torch.nn.utils.clip_grad_norm_(self.amp.master_params(self.optimizer),
                                                           self.args.max_grad_norm)
                            self.optimizer.step()
                    else:
                        torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.args.max_grad_norm)
                        self.optimizer.step()
                    if self.args.do_warmup:
                        self.scheduler.step()
                    # batch_iter.set_postfix({"loss": loss.item(), "tloss": tr_loss/step})
                if self.args.log_step > 0 and (step+1) % self.args.log_step == 0:
                    self.args.logger.info(
                        "epoch: {}; global step: {}; total loss: {}; average loss: {}".format(
                            epoch+1, t_step, tr_loss, tr_loss/t_step))
                if(t_step % 100 == 0):
                    wandb.log({"train_loss/step": tr_loss/t_step}, step=t_step)    
                    loss_dict[t_step] = tr_loss/t_step

                t_step += 1
            batch_iter.close()

            # at each epoch end, we do eval on dev
            if self.args.do_eval:
                acc, pr, f1 = self.eval(self.args.non_relation_label)
                self.args.logger.info("""
                ******************************
                Epcoh: {}
                evaluation on dev set
                acc: {}
                {}; f1:{}
                ******************************
                """.format(epoch+1, acc, pr, f1))
                # max_num_checkpoints > 0, save based on eval
                # save model
                if self.args.max_num_checkpoints > 0 and latest_best_score < f1:
                    self._save_model(epoch+1)
                    latest_best_score = f1
        epoch_iter.close()
        self.model = accelerator.unwrap_model(self.model)

        wandb.finish()
        
        # max_num_checkpoints=0 then save at the end of training
        if self.args.max_num_checkpoints <= 0:
            self._save_model(0)
            self.args.logger.info("training finish and the trained model is saved.")
        #self.args.logger.info("Saving loss in file..{}".format(self.loss_file_path))
        with open(self.loss_file_path, 'wb') as handle:
            pickle.dump(loss_dict, handle, protocol=pickle.HIGHEST_PROTOCOL)

    def eval(self, non_rel_label=""):
        self.args.logger.info("start evaluation...")

        # this is done on dev
        true_labels = np.array([dev_fea.label for dev_fea in self.dev_features])
        preds, eval_loss = self._run_eval(self.dev_data_loader)
        eval_res = acc_and_f1(
            labels=true_labels, preds=preds, label2idx=self.label2idx, non_rel_label=non_rel_label)

        return eval_res

    def predict(self):
        self.args.logger.info("start prediction...")
        # this is for prediction
        preds, _ = self._run_eval(self.test_data_loader)
        # convert predicted label idx to real label
        self.args.logger.info("label to index for prediction:\n{}".format(self.label2idx))
        preds = [self.idx2label[pred] for pred in preds]

        return preds

    def _init_new_model(self):
        """initialize a new model for fine-tuning"""
        self.args.logger.info("Init new model...")

        model, config, tokenizer = self.model_dict[self.args.model_type]

        # init tokenizer and add special tags
        self.tokenizer = tokenizer.from_pretrained(self.args.pretrained_model, do_lower_case=self.args.do_lower_case)
        print("Setting pd_token_______________________")
        
        if getattr(self.tokenizer, "pad_token_id") is None:
            print("assigning custom pad token ---> ", self.tokenizer.eos_token_id)
            self.tokenizer.pad_token_id = self.tokenizer.eos_token_id
            
        last_token_idx = len(self.tokenizer)
        self.tokenizer.add_tokens(SPEC_TAGS)
        spec_token_new_ids = tuple([(last_token_idx + idx) for idx in range(len(self.tokenizer) - last_token_idx)])
        total_token_num = len(self.tokenizer)

        # init config
        unique_labels, label2idx, idx2label = self.data_processor.get_labels()
        self.args.logger.info("label to index:\n{}".format(label2idx))
        save_json(label2idx, self.new_model_dir_path/"label2idx.json")
        num_labels = len(unique_labels)
        self.label2idx = label2idx
        self.idx2label = idx2label

        self.config = config.from_pretrained(self.args.pretrained_model, num_labels=num_labels)
        self.config.torch_dtype = getattr(torch, 'bfloat16')
        print("##############")
        print(self.config)
        print("##############")
        self.config.update({CONFIG_VERSION_NAME: VERSION})
        # The number of tokens to cache.
        # The key/value pairs that have already been pre-computed in a previous forward pass won’t be re-computed.
        if self.args.model_type == "xlnet":
            self.config.mem_len = self.config.d_model
            # change dropout name
            self.config.hidden_dropout_prob = self.config.dropout

        self.config.tags = spec_token_new_ids
        self.config.scheme = self.args.classification_scheme
        # binary mode
        self.config.binary_mode = self.args.use_binary_classification_mode
        # focal loss config
        self.config.use_focal_loss = self.args.use_focal_loss
        self.config.focal_loss_gamma = self.args.focal_loss_gamma
        # sample weights in loss functions
        self.config.balance_sample_weights = self.args.balance_sample_weights
        if self.args.balance_sample_weights:
            label2freq = self.data_processor.get_sample_distribution()
            label_id2freq = {label2idx[k]: v for k, v in label2freq.items()}
            self.config.sample_weights = np.zeros(len(label2freq))
            for k, v in label_id2freq.items():
                self.config.sample_weights[k] = v
            self.args.logger.info(
                f"using sample weights: {label_id2freq} and converted weight matrix is {self.config.sample_weights}")
        
        # init model: modified for llama1, llama2, llama1_pre, llama2_pre
        if(self.args.model_type=="llama1"):
            print("Initialising llama 1 with new peft model ....")
            llamaModel = model.from_pretrained(
                self.args.pretrained_model,
                config=self.config,
                torch_dtype=getattr(torch, 'bfloat16'),
                low_cpu_mem_usage=True,  
            )
            peft_config = LoraConfig(
                task_type=TaskType.SEQ_CLS,
                target_modules=target_modules,
                inference_mode=False,
                r=self.args.lora_rank, lora_alpha=self.args.lora_alpha,
                lora_dropout=lora_dropout,
                modules_to_save=mod_to_save)
            print("#### NEW PEFT config ####")
            print(peft_config)
            print(self.config)
            print("####")
            self.model = get_peft_model(llamaModel, peft_config)
        elif(self.args.model_type=="llama2"):
            print("Initialising llama 2 with new peft model ....")
            llamaModel = model.from_pretrained(
                self.args.pretrained_model,
                cache_dir=mycache_dir,
                config=self.config,
                torch_dtype=getattr(torch, 'bfloat16'),
                low_cpu_mem_usage=True,  
            )
            peft_config = LoraConfig(
                task_type=TaskType.SEQ_CLS,
                target_modules=target_modules,
                inference_mode=False,
                r=self.args.lora_rank, lora_alpha=self.args.lora_alpha,
                lora_dropout=lora_dropout,
                modules_to_save=mod_to_save)
            print("#### NEW PEFT config ####")
            print(peft_config)
            print(self.config)
            print("####")
            self.model = get_peft_model(llamaModel, peft_config)
        elif(self.args.model_type=="llama1_pre"):
            print("Initialising llama 1 from peft model path ....{}".format(LLAMA1_PEFT_MODEL_PATH))
            llamaModel = model.from_pretrained(
                self.args.pretrained_model,
                config=self.config,
                torch_dtype=getattr(torch, 'bfloat16'),
                low_cpu_mem_usage=True,  
            )
            self.model = PeftModel.from_pretrained(llamaModel,LLAMA1_PEFT_MODEL_PATH)
            print("#### Loaded PEFT config ####")
            print(self.config)
            print("####")
        else:
            print("Initialising llama 2 from peft model path ....{}".format(LLAMA2_PEFT_MODEL_PATH))
            llamaModel = model.from_pretrained(
                self.args.pretrained_model,
                cache_dir=mycache_dir,
                config=self.config,
                torch_dtype=getattr(torch, 'bfloat16'),
                low_cpu_mem_usage=True,  
            )
            self.model = PeftModel.from_pretrained(llamaModel,LLAMA2_PEFT_MODEL_PATH)
            print("#### Loaded PEFT config ####")
            print(self.config)
            print("####")
            

        print("----- Loaded model in datatype --- ", getattr(torch, 'bfloat16'))
        self.model.print_trainable_parameters()

        # convert model to bfloat16
        for param in self.model.parameters():
            # Check if parameter dtype is  Float (float32)
            if param.dtype == torch.float32 or param.dtype == torch.float16 :
                param.data = param.data.to(torch.bfloat16)

        # self.model = model.from_pretrained(self.args.pretrained_model, config=self.config)
        self.config.vocab_size = total_token_num
        # resize embedding layer ass we add special tokens
        self.model.resize_token_embeddings(total_token_num)
        print("Model resized to {}".format(total_token_num))
        
        # load model to device 
        print("Model loaded on device ------ ",self.args.device)
        self.model.to(self.args.device)

    def _init_optimizer(self):
        # set up optimizer
        no_decay = ["bias", "LayerNorm.weight"]

        optimizer_grouped_parameters = [
            {'params': [p for n, p in self.model.named_parameters() if not any(nd in n for nd in no_decay)],
             'weight_decay': self.args.weight_decay},
            {'params': [p for n, p in self.model.named_parameters() if any(nd in n for nd in no_decay)],
             'weight_decay': 0.0}
        ]

        self.optimizer = torch.optim.AdamW(optimizer_grouped_parameters,
                                           lr=self.args.learning_rate,
                                           eps=self.args.adam_epsilon)
        self.args.logger.info("The optimizer detail:\n {}".format(self.optimizer))

        # set up optimizer warm up scheduler (you can set warmup_ratio=0 to deactivated this function)
        if self.args.do_warmup:
            t_total = len(self.train_data_loader) // self.args.gradient_accumulation_steps * self.args.num_train_epochs
            warmup_steps = np.dtype('int64').type(self.args.warmup_ratio * t_total)
            self.scheduler = get_linear_schedule_with_warmup(self.optimizer,
                                                             num_warmup_steps=warmup_steps,
                                                             num_training_steps=t_total)

        # mix precision training
        if self.args.fp16 and self._use_amp_for_fp16_from == 2:
            self.model, self.optimizer = self.amp.initialize(self.model, self.optimizer,
                                                             opt_level=self.args.fp16_opt_level)

    def _init_trained_model(self):
        """initialize a fine-tuned model for prediction"""
        self.args.logger.info("Init trained model...")
        model, config, tokenizer = self.model_dict[self.args.model_type]
        
        # Handle separately for (llama1 or llama1_pre) and (llama2_pre or llama2) 
        if(self.args.model_type=="llama1_pre" or self.args.model_type=="llama1" ): 

            # Use the latest checkpoint
            latest_ckpt_dir = Path(self.args.ckpt_dir)

            # load label2idx
            self.label2idx, self.idx2label = pkl_load(latest_ckpt_dir/"label_index.pkl")
            self.args.logger.info("Init model from {} for prediction".format(self.args.pretrained_model))
            num_labels = len(self.label2idx)
            print("Loading trained model and tokeniser from provided path:", latest_ckpt_dir)
            self.config = config.from_pretrained(latest_ckpt_dir, num_labels=num_labels)
            
            self.tokenizer = tokenizer.from_pretrained(latest_ckpt_dir, do_lower_case=self.args.do_lower_case)

            last_token_idx = len(self.tokenizer)
            self.tokenizer.add_tokens(SPEC_TAGS)
            spec_token_new_ids = tuple([(last_token_idx + idx) for idx in range(len(self.tokenizer) - last_token_idx)])
            total_token_num = len(self.tokenizer)

            # Set the pad token is set to eos_token_id
            if getattr(self.tokenizer, "pad_token_id") is None:
                print("assigning custom pad token ---> ", self.tokenizer.eos_token_id)
                self.tokenizer.pad_token_id = self.tokenizer.eos_token_id
            # self.config = PeftConfig.from_pretrained(latest_ckpt_dir,task_type=TaskType.SEQ_CLS)
            self.model = model.from_pretrained(
                                    self.args.pretrained_model,
                                    # cache_dir=mycache_dir,
                                    # num_labels=num_labels,
                                    config=self.config,
                                    torch_dtype=getattr(torch, 'bfloat16'),
                                    low_cpu_mem_usage=True
                                )
            print("#### PEFT CONFIG #####")
            print(self.config)
            print("####")

            # Uncomment when using llama1_pre model for prediction
            # self.model = PeftModel.from_pretrained(self.model,LLAMA1_PEFT_MODEL_PATH,is_trainable=False)
            # self.model.resize_token_embeddings(len(self.tokenizer))

            self.model = PeftModel.from_pretrained(self.model,latest_ckpt_dir,config=self.config)
            self.model.resize_token_embeddings(len(self.tokenizer))
        
            # convert model to bfloat16
            for param in self.model.parameters():
                # Check if parameter dtype is  Float (float32)
                if param.dtype == torch.float32 or param.dtype == torch.float16 :
                    param.data = param.data.to(torch.bfloat16)  

        elif(self.args.model_type=="llama2_pre" or self.args.model_type=="llama2"): 
            
            # Use the latest checkpoint
            latest_ckpt_dir = Path(self.args.ckpt_dir)
            # load label2idx
            self.label2idx, self.idx2label = pkl_load(latest_ckpt_dir/"label_index.pkl")
            self.args.logger.info("Init model from base model {} for prediction".format(self.args.pretrained_model))
            num_labels = len(self.label2idx)

            print("Loading model and tokeniser from provided path:", latest_ckpt_dir)
            self.config = config.from_pretrained(self.args.pretrained_model, num_labels=num_labels)
            
            self.tokenizer = tokenizer.from_pretrained(latest_ckpt_dir, do_lower_case=self.args.do_lower_case)
            
            # Set the pad token is set to eos_token_id
            if getattr(self.tokenizer, "pad_token_id") is None:
                print("assigning custom pad token ---> ", self.tokenizer.eos_token_id)
                self.tokenizer.pad_token_id = self.tokenizer.eos_token_id
            llamaModel = model.from_pretrained(
                                    self.args.pretrained_model,
                                    cache_dir=mycache_dir,
                                    # num_labels=num_labels,
                                    config=self.config,
                                    torch_dtype=getattr(torch, 'bfloat16'),
                                    low_cpu_mem_usage=True,  
                                )
            
            print("#### PEFT CONFIG \#####")
            print(self.config)
            print("####")
            llamaModel.resize_token_embeddings(len(self.tokenizer))

            # Uncomment when using llama2_pre model for prediction  
            # self.model = PeftModel.from_pretrained(llamaModel,LLAMA2_PEFT_MODEL_PATH, is_trainable=False)
            # self.model.resize_token_embeddings(len(self.tokenizer))

            self.model = PeftModel.from_pretrained(llamaModel,latest_ckpt_dir)
            self.model.resize_token_embeddings(len(self.tokenizer))
        
            # convert model to bfloat16
            for param in self.model.parameters():
                # Check if parameter dtype is  Float (float32)
                if param.dtype == torch.float32 or param.dtype == torch.float16 :
                    param.data = param.data.to(torch.bfloat16)    
        else:
            self.args.logger.info("Init model from {} for prediction".format(latest_ckpt_dir))
            # dir_list = [d for d in self.new_model_dir_path.iterdir() if d.is_dir()]
            # latest_ckpt_dir = sorted(dir_list, key=lambda x: int(x.stem.split("_")[-1]))[-1]
            self.config = config.from_pretrained(latest_ckpt_dir)
            # compatibility check for config arguments
            if not (self.config.to_dict().get(CONFIG_VERSION_NAME, None) == VERSION):
                self.config.update(NEW_ARGS)

            self.tokenizer = tokenizer.from_pretrained(latest_ckpt_dir, do_lower_case=self.args.do_lower_case)
            if getattr(self.tokenizer, "pad_token_id") is None:
                print("assigning EOS token as PAD token ---> ", self.tokenizer.eos_token_id)
                self.tokenizer.pad_token_id = self.tokenizer.eos_token_id

            self.model = model.from_pretrained(latest_ckpt_dir, config=self.config)

            # load label2idx
            self.label2idx, self.idx2label = pkl_load(latest_ckpt_dir/"label_index.pkl")
        # load model to device
        self.model.to(self.args.device)

    def _load_amp_for_fp16(self):
        # first try to load PyTorch naive amp; if fail, try apex; if fail again, throw a RuntimeError
        if version.parse(torch.__version__) >= version.parse("1.6.0"):
            self.amp = torch.cuda.amp
            self._use_amp_for_fp16_from = 1
            self.amp_scaler = torch.cuda.amp.GradScaler()
        else:
            try:
                from apex import amp
                self.amp = amp
                self._use_amp_for_fp16_from = 2
            except ImportError:
                self.args.logger.error("apex (https://www.github.com/nvidia/apex) for fp16 training is not installed.")
            finally:
                self.args.fp16 = False

    def _save_model(self, epoch=0):
        dir_to_save = self.new_model_dir_path / f"ckpt_{epoch}"

        self.tokenizer.save_pretrained(dir_to_save)
        self.config.save_pretrained(dir_to_save)
        self.model.save_pretrained(dir_to_save)
        # save label2idx
        pkl_save((self.label2idx, self.idx2label), dir_to_save/"label_index.pkl")
        # remove extra checkpoints
        dir_list = [d for d in self.new_model_dir_path.iterdir() if d.is_dir()]
        if len(dir_list) > self.args.max_num_checkpoints > 0:
            oldest_ckpt_dir = sorted(dir_list, key=lambda x: int(x.stem.split("_")[-1]))[0]
            shutil.rmtree(oldest_ckpt_dir)

    def _run_eval(self, data_loader):
        temp_loss = .0
        # set model to evaluate mode
        self.model.eval()
        accelerator = Accelerator()
        data_loader, self.model = accelerator.prepare(data_loader, self.model)

        # create dev data batch iteration
        batch_iter = tqdm(data_loader, desc="Batch", disable=not self.args.progress_bar)
        total_sample_num = len(batch_iter)
        preds = None
        for batch in batch_iter:
            batch_input = batch_to_model_input(batch, model_type=self.args.model_type, device=self.args.device)
            with torch.no_grad():
                batch_output = self.model(**batch_input)
                loss, logits = batch_output[:2]
                temp_loss += loss.item()
                #wandb.log({"test_loss/step": }, step=t_step) 
                logits = logits.detach().cpu().to(torch.float16).numpy()
                if preds is None:
                    preds = logits
                else:
                    preds = np.append(preds, logits, axis=0)

        batch_iter.close()
        temp_loss = temp_loss / total_sample_num
        preds = np.argmax(preds, axis=-1)

        return preds, temp_loss

    def _load_examples_by_task(self, task="train"):
        examples = None

        if task == "train":
            examples = self.data_processor.get_train_examples()
        elif task == "dev":
            examples = self.data_processor.get_dev_examples()
        elif task == "test":
            examples = self.data_processor.get_test_examples()
        else:
            raise RuntimeError("expect task to be train, dev or test but get {}".format(task))

        return examples

    def _check_cache(self, task="train"):
        cached_examples_file = Path(self.args.data_dir) / "cached_{}_{}_{}_{}_{}.pkl".format(
            self.args.model_type, self.args.data_format_mode, self.args.max_seq_length,
            self.tokenizer.name_or_path.split("/")[-1], task)
        # load examples from files or cache
        if self.args.cache_data and cached_examples_file.exists():
            examples = pkl_load(cached_examples_file)
            self.args.logger.info("load {} data from cached file: {}".format(task, cached_examples_file))
        elif self.args.cache_data and not cached_examples_file.exists():
            self.args.logger.info(
                "create {} examples...and will cache the processed data at {}".format(task, cached_examples_file))
            examples = self._load_examples_by_task(task)
            pkl_save(examples, cached_examples_file)
        else:
            self.args.logger.info("create training examples..."
                                  "the processed data will not be cached")
            examples = self._load_examples_by_task(task)
        return examples

    def reset_dataloader(self, data_dir, has_file_header=None, max_len=None):
        """
          allow reset data dir and data file header and max seq len
        """
        self.data_processor.set_data_dir(data_dir)
        if has_file_header:
            self.data_processor.set_header(has_file_header)
        if max_len and isinstance(max_len, int):
            self.data_processor.set_max_seq_len(max_len)
        self.args.logger.warning("reset data loader information")
        self.args.logger.warning("new data loader info: {}".format(self.data_processor))
        self.test_data_loader = None
        self._init_dataloader()

    def _init_dataloader(self):
        if self.args.do_train and self.train_data_loader is None:
            train_examples = self._check_cache(task="train")
            # convert examples to tensor
            train_features = convert_examples_to_relation_extraction_features(
                train_examples,
                tokenizer=self.tokenizer,
                max_length=self.args.max_seq_length,
                label_list=self.label2idx,
                output_mode="classification")

            self.train_data_loader = relation_extraction_data_loader(
                train_features,
                batch_size=self.args.train_batch_size,
                task="train",
                logger=self.args.logger,
                binary_mode=self.args.use_binary_classification_mode)

        if self.args.do_eval and self.dev_data_loader is None:
            dev_examples = self._check_cache(task="dev")
            # example2feature
            dev_features = convert_examples_to_relation_extraction_features(
                dev_examples,
                tokenizer=self.tokenizer,
                max_length=self.args.max_seq_length,
                label_list=self.label2idx,
                output_mode="classification")
            self.dev_features = dev_features

            self.dev_data_loader = relation_extraction_data_loader(
                dev_features,
                batch_size=self.args.train_batch_size,
                task="test",
                logger=self.args.logger,
                binary_mode=self.args.use_binary_classification_mode)

        if self.args.do_predict and self.test_data_loader is None:
            test_examples = self._check_cache(task="test")
            # example2feature
            print("label2idx in test data loader:")
            print(self.label2idx)
            print("use binary classi:", self.args.use_binary_classification_mode)
            test_features = convert_examples_to_relation_extraction_features(
                test_examples,
                tokenizer=self.tokenizer,
                max_length=self.args.max_seq_length,
                label_list=self.label2idx,
                output_mode="classification")

            self.test_data_loader = relation_extraction_data_loader(
                test_features,
                batch_size=self.args.eval_batch_size,
                task="test", logger=self.args.logger,
                binary_mode=self.args.use_binary_classification_mode)
