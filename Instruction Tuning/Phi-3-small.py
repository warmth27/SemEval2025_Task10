import json
import pandas as pd
import torch
from datasets import Dataset
from peft import LoraConfig, TaskType, get_peft_model
from transformers import AutoModelForCausalLM, TrainingArguments, Trainer, DataCollatorForSeq2Seq,AutoTokenizer
import os

all_fine_grained_roles_list= ["Guardian", "Martyr", "Peacemaker", "Rebel", "Underdog", 
                              "Virtuous","Instigator", "Conspirator", "Tyrant", "Foreign Adversary", 
                              "Traitor", "Spy", "Saboteur", "Corrupt", "Incompetent","Terrorist", "Deceiver", 
                              "Bigot","Forgotten", "Exploited", "Victim", "Scapegoat"]


def dataset_jsonl_transfer(origin_path, new_path):
    """
    将原始数据集转换为大模型微调所需数据格式的新数据集
    """
    messages = []
    # 读取旧的JSONL文件
    with open(origin_path, "r", encoding='UTF-8') as file:
        for line in file:
            # 解析每一行的json数据
            data = json.loads(line)
            context = data["text"]
            entity_mention = data["entity"] #实体
            answer = data["fine_grained_role"]
            message = {
                "instruction":f"You are an expert in the field of multi label classification. Given an article and an entity within that article. What you need to do is analyze this article and the entity, and provide the fine-grained roles of the entity. There are more than one fine-grained role. If there are multiple roles, they should be followed directly in the output, separated by spaces.List of fine-grained role:{all_fine_grained_roles_list}",
                "input": f"article:{context},entity:{entity_mention},",
                "output": answer,
            }
            messages.append(message)

    # 保存重构后的JSONL文件
    with open(new_path, "w", encoding="utf-8") as file:
        for message in messages:
            file.write(json.dumps(message, ensure_ascii=False) + "\n")

def process_func(example):
    """
    将数据集进行预处理
    """
    MAX_LENGTH = 2048
    input_ids, attention_mask, labels = [], [], []
    instruction = tokenizer(
        f"<|im_start|>system\nYou are an expert in the field of multi label classification. Given an article and an entity within that article. What you need to do is analyze this article and the entity, and provide the fine-grained roles of the entity. There are more than one fine-grained role. If there are multiple roles, they should be followed directly in the output, separated by spaces.List of fine-grained role:{all_fine_grained_roles_list}<|im_end|>\n<|im_start|>user\n{example['input']}<|im_end|>\n<|im_start|>assistant\n",
        add_special_tokens=False,
    )
    response = tokenizer(f"{example['output']}", add_special_tokens=False)
    input_ids = instruction["input_ids"] + response["input_ids"] + [tokenizer.pad_token_id]
    attention_mask = (
        instruction["attention_mask"] + response["attention_mask"] + [1]
    )
    labels = [-100] * len(instruction["input_ids"]) + response["input_ids"] + [tokenizer.pad_token_id]
    if len(input_ids) > MAX_LENGTH:  # 做一个截断
        input_ids = input_ids[:MAX_LENGTH]
        attention_mask = attention_mask[:MAX_LENGTH]
        labels = labels[:MAX_LENGTH]
    return {"input_ids": input_ids, "attention_mask": attention_mask, "labels": labels}   


def predict(messages, model, tokenizer):
    device = "cuda"
    temperature=0.2
    text = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True
    )
    model_inputs = tokenizer([text], return_tensors="pt").to(device)

    generated_ids = model.generate(
        model_inputs.input_ids,
        max_new_tokens=512,
        temperature=temperature
    )
    generated_ids = [
        output_ids[len(input_ids):] for input_ids, output_ids in zip(model_inputs.input_ids, generated_ids)
    ]

    response = tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]
    print(response)
    with open("result_epoch_auto.txt", "a",encoding='utf-8') as f:
        f.writelines(response)
        f.write("\n")
    return response



# Transformers加载模型权重
tokenizer = AutoTokenizer.from_pretrained("/media/qust521/92afc6a9-13fd-4458-8a46-4b008127de08/LLMs/Phi-3-small-128k-instruct", use_fast=False, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained("/media/qust521/92afc6a9-13fd-4458-8a46-4b008127de08/LLMs/Phi-3-small-128k-instruct", 
                                             device_map="auto", torch_dtype=torch.bfloat16,trust_remote_code=True)

model.enable_input_require_grads()  # 开启梯度检查点时，要执行该方法

# 加载、处理数据集和测试集
train_dataset_path = "train.jsonl"
test_dataset_path = "test.jsonl"

train_jsonl_new_path = "new_train.jsonl"
test_jsonl_new_path = "new_test.jsonl"

if not os.path.exists(train_jsonl_new_path):
    dataset_jsonl_transfer(train_dataset_path, train_jsonl_new_path)
if not os.path.exists(test_jsonl_new_path):
    dataset_jsonl_transfer(test_dataset_path, test_jsonl_new_path)

# 得到训练集
train_df = pd.read_json(train_jsonl_new_path, lines=True)
train_ds = Dataset.from_pandas(train_df)
train_dataset = train_ds.map(process_func, remove_columns=train_ds.column_names)

config = LoraConfig(
    task_type=TaskType.CAUSAL_LM,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    inference_mode=False,  # 训练模式
    r=8,  # Lora 秩
    lora_alpha=32,  # Lora alaph，具体作用参见 Lora 原理
    lora_dropout=0.1,  # Dropout 比例
)

model = get_peft_model(model, config)

args = TrainingArguments(
    output_dir="./output/phi3_train-dev20",
    per_device_train_batch_size=1,
    gradient_accumulation_steps=8,
    logging_steps=10,
    num_train_epochs=20,
    save_strategy="epoch",
    learning_rate=1e-4,
    save_on_each_node=True,
    gradient_checkpointing=True,
    report_to="none",
)

trainer = Trainer(
    model=model,
    args=args,
    train_dataset=train_dataset,
    data_collator=DataCollatorForSeq2Seq(tokenizer=tokenizer, padding=True),
)

trainer.train()

# 用测试集的前10条，测试模型
test_df = pd.read_json(test_jsonl_new_path, lines=True)

for index, row in test_df.iterrows():
    instruction = row['instruction']
    input_value = row['input']
    messages = [
        {"role": "system", "content": f"{instruction}"},
        {"role": "user", "content": f"{input_value}"}
    ]
    response = predict(messages, model, tokenizer)
