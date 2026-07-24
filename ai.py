import json
import random
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
import numpy as np
from sklearn.metrics import accuracy_score, f1_score
import re
import time
from collections import defaultdict
from pathlib import Path

# -------------------- 配置管理 --------------------
class Config:
    def __init__(self):
        self.MAX_LEN = 64
        self.BATCH_SIZE = 16
        self.EPOCHS = 50
        self.LEARNING_RATE = 1e-3
        self.DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.THRESHOLD = 0.5
        self.MIN_CONFIDENCE = 0.7  # 最低置信度阈值
        self.INTENTS = ["开关灯", "空调开关", "空调制热", "空调制冷", "查环境", "温度调节", "定时控制", "模式切换", "风速调节"]
        self.NUM_LABELS = len(self.INTENTS)
        self.SEED = 42

config = Config()

# 固定随机种子
def set_seed(seed=config.SEED):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

set_seed()


# -------------------- 文本归一化 --------------------
def normalize_text(text):
    """修正常见语音识别错字，让模型和规则看到更稳定的命令文本。"""
    text = str(text).strip().lower()
    replacements = {
        "空条": "空调",
        "控调": "空调",
        "口调": "空调",
        "风数": "风速",
        "疯速": "风速",
        "风俗": "风速",
        "风宿": "风速",
        "风挡": "风档",
        "风当": "风档",
        "膏风": "高风",
        "糕风": "高风",
        "高峰": "高风",
        "膏速": "高速",
        "糕速": "高速",
        "高速风": "高风",
        "钟风": "中风",
        "中锋": "中风",
        "钟速": "中速",
        "调成钟": "调成中",
        "调到钟": "调到中",
        "风速钟": "风速中",
        "风量钟": "风量中",
        "风力钟": "风力中",
        "钟档": "中档",
        "钟挡": "中档",
        "低俗": "低速",
        "直冷": "制冷",
        "智冷": "制冷",
        "治冷": "制冷",
        "直热": "制热",
        "致热": "制热",
        "智热": "制热",
        "开致热": "开制热",
        "开直热": "开制热",
        "开智热": "开制热",
        "开直冷": "开制冷",
        "开智冷": "开制冷",
        "开治冷": "开制冷",
        "开智能": "开制冷",
        "打开智能": "打开制冷",
        "空调智能": "空调制冷",
        "智能模式": "制冷模式",
        "制能": "制冷",
        "智冷": "制冷",
        "只能": "制冷",
        "除是": "除湿",
        "除事": "除湿",
        "楚湿": "除湿",
        "去湿": "除湿",
        "祛湿": "除湿",
        "抽湿": "除湿",
        "送疯": "送风",
        "松风": "送风",
        "松峰": "送风",
        "通风模式": "送风模式",
        "吹风模式": "送风模式",
        "风扇模式": "送风模式",
        "官灯": "关灯",
        "管灯": "关灯",
        "馆灯": "关灯",
        "开等": "开灯",
        "关等": "关灯",
        "卧室等": "卧室灯",
        "客厅等": "客厅灯",
    }
    for wrong, right in replacements.items():
        text = text.replace(wrong, right)

    # 只在智能家居命令上下文里把单字“膏/糕”当成“高”，避免误伤普通文本。
    command_context = any(word in text for word in ["风", "速", "档", "挡", "温度", "空调"])
    if command_context:
        text = text.replace("膏", "高").replace("糕", "高")

    return re.sub(r"\s+", "", text)

# -------------------- 1. 数据管理模块 --------------------
class DataManager:
    def __init__(self):
        # 动作指令模板
        self.action_templates = {
            "开关灯": ["开灯", "关灯", "打开灯", "把灯关了", "关一下灯", "开一下灯", "帮我开灯", "帮我关灯", "打开客厅灯", "关闭卧室灯", "灯光打开", "灯光关闭"],
            "空调开关": ["开空调", "关空调", "打开空调", "关闭空调", "空调开机", "空调关机", "启动空调", "把空调关了", "空调停一下"],
            "空调制热": ["开制热", "打开制热", "开暖风", "制热模式", "空调制热", "帮我开制热", "开暖气", "太冷了开暖风", "冷的话开制热"],
            "空调制冷": ["开制冷", "打开制冷", "开冷气", "制冷模式", "空调制冷", "帮我开制冷", "开空调制冷", "太热了开冷气", "热的话开制冷", "开智能", "打开智能模式", "空调智能", "智能模式", "只能模式"],
            "查环境": ["查环境", "环境怎么样", "温度多少", "湿度多少", "查一下环境", "看看环境", "环境信息", "现在室温多少", "房间热不热", "看看室内环境", "空气质量怎么样", "现在湿度是多少"],
            "温度调节": ["调温度", "温度调节", "调节温度", "调整温度", "温度高一点", "温度低一点", "升高温度", "降低温度"],
            "定时控制": ["定时", "设置定时", "定时开关", "预约", "十分钟后关灯", "半小时后关空调", "五分钟后开制冷"],
            "模式切换": ["切换模式", "模式切换", "更改模式", "设置模式", "开除湿", "打开除湿", "去湿", "祛湿", "抽湿", "除湿模式", "打开送风", "开送风", "送风模式", "吹风模式", "通风模式", "风扇模式", "自动模式", "睡眠模式"],
            "风速调节": ["调风速", "风速调节", "调节风速", "调整风速", "风速设置", "风量调一下", "风力调大", "风速调成中", "风速调膏", "膏风"]
        }

        # 感受类模板
        self.feeling_templates = {
            "空调制热": ["好冷", "太冷了", "冷死了", "有点冷", "好冷啊", "冻死了", "我好冷", "我太冷了"],
            "空调制冷": ["好热", "太热了", "热死了", "有点热", "好热啊", "热得不行", "我好热", "我太热了"]
        }

        # 温度调节模板
        self.temperature_templates = [
            "把温度调到{}度", "温度调到{}度", "调到{}度",
            "把温度调{}一点", "温度调{}一点", "调{}一点", "温度{}一点"
        ]

        # 定时控制模板
        self.timer_templates = [
            "{}小时后{}", "{}分钟后{}", "定时{}小时", "定时{}分钟",
            "{}点{}", "{}时{}"
        ]

        # 模式切换模板
        self.mode_templates = [
            "切换到{}模式", "设置为{}模式", "打开{}模式", "启用{}模式"
        ]

        # 风速调节模板
        self.wind_templates = [
            "把风速调到{}", "风速调到{}", "调到{}风速",
            "风速{}一点", "调{}风速", "风速设置为{}", "风量调到{}", "风力调成{}", "把风档调成{}"
        ]

        self.temperature_values = ["16", "17", "18", "19", "20", "21", "22", "23", "24", "25", "26", "27", "28", "29", "30"]
        self.temperature_adjustments = ["高", "低", "高", "低", "高", "低", "高", "低"]
        self.timer_values = ["1", "2", "3", "4", "5", "10", "15", "20", "30", "60"]
        self.timer_actions = ["开灯", "关灯", "开空调", "关空调", "开制冷", "开制热", "开除湿", "开送风", "调中风"]
        self.timer_hours = ["6", "7", "8", "9", "10", "11", "12", "13", "14", "15", "16", "17", "18", "19", "20", "21", "22", "23"]
        self.modes = ["自动", "睡眠", "节能", "舒适", "强劲", "除湿", "送风"]
        self.wind_speeds = ["低速", "中速", "高速", "低风", "中风", "高风", "自动", "最大", "最小"]
        self.asr_noise_pairs = [
            ("高", "膏"), ("高", "糕"), ("制热", "直热"), ("制热", "致热"),
            ("制冷", "直冷"), ("制冷", "智能"), ("除湿", "去湿"), ("除湿", "楚湿"),
            ("送风", "松风"), ("送风", "送疯"), ("空调", "空条"), ("风速", "风俗"),
            ("关灯", "管灯"), ("开灯", "开等"), ("中速", "钟速"), ("高风", "膏风")
        ]

    def labels_for(self, intents):
        return [1 if intent in intents else 0 for intent in config.INTENTS]

    def add_asr_noise(self, text):
        noisy = text
        random.shuffle(self.asr_noise_pairs)
        for source, target in self.asr_noise_pairs[:2]:
            if source in noisy and random.random() < 0.7:
                noisy = noisy.replace(source, target, 1)
        return noisy

    def curated_samples(self):
        rows = [
            ("开灯", ["开关灯"]),
            ("关灯", ["开关灯"]),
            ("打开客厅灯", ["开关灯"]),
            ("关闭卧室灯", ["开关灯"]),
            ("开空调", ["空调开关"]),
            ("关空调", ["空调开关"]),
            ("把空调打开", ["空调开关"]),
            ("把空调关了", ["空调开关"]),
            ("开制热", ["空调制热"]),
            ("打开制热模式", ["空调制热"]),
            ("开直热", ["空调制热"]),
            ("太冷了开暖风", ["空调制热"]),
            ("开制冷", ["空调制冷"]),
            ("打开制冷模式", ["空调制冷"]),
            ("开直冷", ["空调制冷"]),
            ("开智能", ["空调制冷"]),
            ("打开智能模式", ["空调制冷"]),
            ("太热了开冷气", ["空调制冷"]),
            ("温度调高一点", ["温度调节"]),
            ("温度调膏一点", ["温度调节"]),
            ("把温度调到26度", ["温度调节"]),
            ("风速调成中", ["风速调节"]),
            ("风俗调成中", ["风速调节"]),
            ("把风速调膏", ["风速调节"]),
            ("风量调成高", ["风速调节"]),
            ("膏风", ["风速调节"]),
            ("切换到除湿模式", ["模式切换"]),
            ("打开送风模式", ["模式切换"]),
            ("开除湿", ["模式切换"]),
            ("打开除湿", ["模式切换"]),
            ("去湿", ["模式切换"]),
            ("祛湿", ["模式切换"]),
            ("楚湿模式", ["模式切换"]),
            ("开送风", ["模式切换"]),
            ("打开送风", ["模式切换"]),
            ("松风模式", ["模式切换"]),
            ("送疯模式", ["模式切换"]),
            ("吹风模式", ["模式切换"]),
            ("通风模式", ["模式切换"]),
            ("查一下房间温度", ["查环境"]),
            ("现在湿度多少", ["查环境"]),
            ("十分钟后关灯", ["定时控制", "开关灯"]),
            ("半小时后关空调", ["定时控制", "空调开关"]),
            ("晚上八点开制热", ["定时控制", "空调制热"]),
            ("开灯并打开制冷", ["开关灯", "空调制冷"]),
            ("关灯并且开制热", ["开关灯", "空调制热"]),
            ("风速调成中并且开制热", ["风速调节", "空调制热"]),
            ("风俗调成钟并且开直热", ["风速调节", "空调制热"]),
            ("把风速调膏并打开制冷", ["风速调节", "空调制冷"]),
            ("打开除湿并把风速调低", ["模式切换", "风速调节"]),
            ("开送风并把风速调成中", ["模式切换", "风速调节"]),
            ("松风模式并调高风", ["模式切换", "风速调节"]),
            ("打开除湿并调到低风", ["模式切换", "风速调节"]),
            ("开送风然后风速调中", ["模式切换", "风速调节"]),
            ("查看环境然后开除湿", ["查环境", "模式切换"]),
            ("湿度太高打开除湿", ["查环境", "模式切换"]),
            ("五分钟后关灯", ["定时控制", "开关灯"]),
            ("十分钟后打开送风", ["定时控制", "模式切换"]),
            ("半小时后开除湿", ["定时控制", "模式切换"]),
            ("开空调并把温度调到24度", ["空调开关", "温度调节"]),
            ("关灯然后关空调", ["开关灯", "空调开关"]),
        ]
        return [{"text": text, "labels": self.labels_for(intents)} for text, intents in rows]

    def generate_sample(self):
        # 随机选择意图数量
        num_intents = random.randint(1, 2)
        selected_intents = random.sample(config.INTENTS, num_intents)
        
        # 生成文本
        texts = []
        for intent in selected_intents:
            if intent == "温度调节":
                # 生成温度调节指令
                if random.random() < 0.5:
                    # 具体温度值
                    temp = random.choice(self.temperature_values)
                    template = random.choice(self.temperature_templates[:3])
                    texts.append(template.format(temp))
                else:
                    # 相对调节
                    adjustment = random.choice(self.temperature_adjustments)
                    template = random.choice(self.temperature_templates[3:])
                    texts.append(template.format(adjustment))
            elif intent == "定时控制":
                # 生成定时控制指令
                if random.random() < 0.5:
                    # 相对时间
                    timer = random.choice(self.timer_values)
                    action = random.choice(self.timer_actions)
                    template = random.choice(self.timer_templates[:2])
                    texts.append(template.format(timer, action))
                else:
                    # 绝对时间
                    hour = random.choice(self.timer_hours)
                    action = random.choice(self.timer_actions)
                    template = random.choice(self.timer_templates[4:])
                    texts.append(template.format(hour, action))
            elif intent == "模式切换":
                # 生成模式切换指令
                mode = random.choice(self.modes)
                template = random.choice(self.mode_templates)
                texts.append(template.format(mode))
            elif intent == "风速调节":
                # 生成风速调节指令
                speed = random.choice(self.wind_speeds)
                template = random.choice(self.wind_templates)
                texts.append(template.format(speed))
            elif intent in self.feeling_templates and random.random() < 0.5:
                texts.append(random.choice(self.feeling_templates[intent]))
            else:
                texts.append(random.choice(self.action_templates[intent]))
        
        text = "，".join(texts)
        if random.random() < 0.25:
            text = self.add_asr_noise(text)
        labels = self.labels_for(selected_intents)
        return text, labels

    def generate_dataset(self, num_samples=3000, val_ratio=0.1):
        # 生成样本
        samples = self.curated_samples()
        for _ in range(num_samples):
            text, labels = self.generate_sample()
            samples.append({"text": text, "labels": labels})

        # 划分训练/验证
        random.shuffle(samples)
        split_idx = int(len(samples) * (1 - val_ratio))
        train_data = samples[:split_idx]
        val_data = samples[split_idx:]

        print(f"生成完成: 训练集 {len(train_data)} 条, 验证集 {len(val_data)} 条")
        return train_data, val_data

    def build_vocab(self, texts, max_vocab_size=1000):
        char_counts = {}
        for text in texts:
            for char in text:
                char_counts[char] = char_counts.get(char, 0) + 1
        
        # 按频率排序，取前max_vocab_size个字符
        sorted_chars = sorted(char_counts.items(), key=lambda x: x[1], reverse=True)
        vocab = {char: i+1 for i, (char, _) in enumerate(sorted_chars[:max_vocab_size])}
        vocab['<PAD>'] = 0
        return vocab

    def text_to_features(self, text, vocab, max_len):
        text = normalize_text(text)
        features = []
        for char in text:
            features.append(vocab.get(char, 0))
        
        # 填充或截断
        if len(features) > max_len:
            features = features[:max_len]
        else:
            features += [0] * (max_len - len(features))
        
        return features

# -------------------- 2. 自定义数据集 --------------------
class IntentDataset(Dataset):
    def __init__(self, data, vocab, max_len):
        self.data = data
        self.vocab = vocab
        self.max_len = max_len

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        item = self.data[idx]
        text = item["text"]
        labels = item["labels"]
        features = text_to_features_fast(text, self.vocab, self.max_len)
        return {
            'input_ids': torch.tensor(features, dtype=torch.long),
            'labels': torch.tensor(labels, dtype=torch.float)
        }

# -------------------- 3. 模型定义 --------------------
class AIIntentClassifier(nn.Module):
    def __init__(self, vocab_size, embedding_dim, max_len, num_labels):
        super().__init__()
        self.embedding = nn.Embedding(vocab_size, embedding_dim)
        self.conv1 = nn.Conv1d(embedding_dim, 64, kernel_size=3, padding=1)
        self.conv2 = nn.Conv1d(64, 128, kernel_size=3, padding=1)
        self.pool = nn.MaxPool1d(2)
        # 计算两次池化后的序列长度
        after_pool = max_len // 2 // 2
        self.fc1 = nn.Linear(128 * after_pool, 256)
        self.dropout = nn.Dropout(0.5)
        self.fc2 = nn.Linear(256, num_labels)

    def forward(self, input_ids, attention_mask=None):
        embedded = self.embedding(input_ids)
        embedded = embedded.permute(0, 2, 1)  # (batch, embedding_dim, seq_len)
        
        x = torch.relu(self.conv1(embedded))
        x = self.pool(x)
        x = torch.relu(self.conv2(x))
        x = self.pool(x)
        
        x = x.view(x.size(0), -1)  # 展平
        x = torch.relu(self.fc1(x))
        x = self.dropout(x)
        logits = self.fc2(x)
        return logits

# -------------------- 4. 参数提取模块 --------------------
class ParameterExtractor:
    @staticmethod
    def extract_parameters(text):
        """提取文本中的参数"""
        text = normalize_text(text)
        params = {}
        
        # 提取温度值
        temp_match = re.search(r'([0-9]+)度', text)
        if temp_match:
            params['temperature'] = int(temp_match.group(1))
        
        # 提取温度调节方向。风速里的“高/低”不要误当成温度调节。
        wind_context = any(word in text for word in ['风速', '风量', '风力', '风档', '风挡', '高风', '低风', '中风'])
        temp_context = any(word in text for word in ['温度', '升温', '降温'])
        if temp_context and any(word in text for word in ['高', '高一点', '升高', '调高']):
            params['adjustment'] = 'increase'
        elif temp_context and any(word in text for word in ['低', '低一点', '降低', '调低']):
            params['adjustment'] = 'decrease'
        
        # 提取定时信息
        timer_match = re.search(r'([0-9]+)小时', text)
        if timer_match:
            params['timer_hours'] = int(timer_match.group(1))
        
        timer_match = re.search(r'([0-9]+)分钟', text)
        if timer_match:
            params['timer_minutes'] = int(timer_match.group(1))
        
        # 提取时间点
        time_match = re.search(r'([0-9]+)[点时]', text)
        if time_match:
            params['time_hour'] = int(time_match.group(1))
        
        # 提取模式
        modes = ["自动", "睡眠", "节能", "舒适", "强劲", "除湿", "送风", "制冷", "制热"]
        for mode in modes:
            if mode in text:
                params['mode'] = mode
                break
        
        # 提取风速
        wind_speeds = {
            "低速": "low", "低风": "low", "小风": "low", "最小": "low",
            "中速": "mid", "中风": "mid", "中档": "mid", "中等": "mid",
            "高速": "high", "高风": "high", "大风": "high", "强风": "high", "最大": "high",
            "自动": "auto",
        }
        for speed, value in wind_speeds.items():
            if speed in text:
                params['wind_speed'] = value
                break
        
        return params

# -------------------- 5. 训练与评估函数 --------------------
def train_epoch(model, dataloader, optimizer, criterion):
    model.train()
    total_loss = 0
    for batch in dataloader:
        input_ids = batch['input_ids'].to(config.DEVICE)
        labels = batch['labels'].to(config.DEVICE)

        optimizer.zero_grad()
        logits = model(input_ids)
        loss = criterion(logits, labels)
        loss.backward()
        optimizer.step()
        total_loss += loss.item()
    return total_loss / len(dataloader)

def evaluate(model, dataloader, criterion):
    model.eval()
    total_loss = 0
    all_labels = []
    all_preds = []
    
    with torch.no_grad():
        for batch in dataloader:
            input_ids = batch['input_ids'].to(config.DEVICE)
            labels = batch['labels'].to(config.DEVICE)
            logits = model(input_ids)
            loss = criterion(logits, labels)
            total_loss += loss.item()
            
            probs = torch.sigmoid(logits).cpu().numpy()
            preds = (probs >= config.THRESHOLD).astype(int)
            all_labels.extend(labels.cpu().numpy())
            all_preds.extend(preds)
    
    all_labels = np.array(all_labels)
    all_preds = np.array(all_preds)
    accuracy = accuracy_score(all_labels, all_preds)
    f1 = f1_score(all_labels, all_preds, average='micro')
    
    return total_loss / len(dataloader), accuracy, f1

# -------------------- 6. 预测与缓存模块 --------------------
class PredictionCache:
    def __init__(self, max_size=1000):
        self.cache = {}  # key: text, value: (intent, probs, params, timestamp)
        self.max_size = max_size

    def get(self, text):
        if text in self.cache:
            intent, probs, params, timestamp = self.cache[text]
            # 检查缓存是否过期（1小时）
            if time.time() - timestamp < 3600:
                return intent, probs, params
        return None

    def set(self, text, intent, probs, params):
        if len(self.cache) >= self.max_size:
            # 删除最旧的缓存
            oldest = min(self.cache.items(), key=lambda x: x[1][3])
            del self.cache[oldest[0]]
        self.cache[text] = (intent, probs, params, time.time())

# 全局缓存实例
prediction_cache = PredictionCache()


def text_to_features_fast(text, vocab, max_len):
    text = normalize_text(text)
    features = [vocab.get(char, 0) for char in text]
    if len(features) < max_len:
        features.extend([0] * (max_len - len(features)))
    else:
        features = features[:max_len]
    return features


def predict(text, model, vocab, max_len=config.MAX_LEN):
    text = normalize_text(text)
    # 检查缓存
    cached_result = prediction_cache.get(text)
    if cached_result:
        return cached_result
    
    features = text_to_features_fast(text, vocab, max_len)
    input_ids = torch.tensor([features], dtype=torch.long).to(config.DEVICE)
    
    with torch.no_grad():
        logits = model(input_ids)
        probs = torch.sigmoid(logits).detach().cpu()[0].tolist()
        preds = [1 if prob >= config.THRESHOLD else 0 for prob in probs]
    
    active = [config.INTENTS[i] for i, v in enumerate(preds) if v == 1]
    params = ParameterExtractor.extract_parameters(text)
    
    # 保存到缓存
    prediction_cache.set(text, active, probs, params)
    
    return active, probs, params

# -------------------- 7. AI 解释函数 --------------------
def ai_explanation(text, probs, params=None):
    """生成AI预测的解释"""
    explanations = []
    
    # 检查置信度
    high_confidence_intents = []
    for intent, prob in zip(config.INTENTS, probs):
        if prob > config.THRESHOLD:
            if prob > config.MIN_CONFIDENCE:
                explanations.append(f"检测到 '{intent}' 意图，置信度 {prob:.2f}")
                high_confidence_intents.append(intent)
            else:
                explanations.append(f"可能是 '{intent}' 意图，置信度 {prob:.2f}")
    
    # 添加参数解释
    if params:
        if 'temperature' in params:
            explanations.append(f"识别到温度值: {params['temperature']}度")
        if 'adjustment' in params:
            direction = "升高" if params['adjustment'] == 'increase' else "降低"
            explanations.append(f"识别到调节方向: {direction}温度")
        if 'timer_hours' in params:
            explanations.append(f"识别到定时: {params['timer_hours']}小时后")
        if 'timer_minutes' in params:
            explanations.append(f"识别到定时: {params['timer_minutes']}分钟后")
        if 'time_hour' in params:
            explanations.append(f"识别到时间点: {params['time_hour']}点")
        if 'mode' in params:
            explanations.append(f"识别到模式: {params['mode']}模式")
        if 'wind_speed' in params:
            explanations.append(f"识别到风速: {params['wind_speed']}")

    # 低置信度提示
    if not high_confidence_intents:
        explanations.append("提示：识别结果置信度较低，可能需要更明确的指令")
    
    if not explanations:
        return "未检测到明确意图"
    else:
        return "；".join(explanations)

# -------------------- 8. 主程序 --------------------
def main():
    print(f"使用设备: {config.DEVICE}")
    print("使用基于深度学习的AI意图识别系统")
    print("该系统使用神经网络模型进行意图识别，体现了人工智能的核心特性")
    print("="*60)

    # 初始化数据管理器
    data_manager = DataManager()
    
    # 生成数据集
    print("正在生成数据集...")
    train_raw, val_raw = data_manager.generate_dataset()
    
    # 构建词汇表
    print("正在构建词汇表...")
    all_texts = [item["text"] for item in train_raw]
    vocab = data_manager.build_vocab(all_texts)
    with open("vocab.json", "w", encoding="utf-8") as f:
        json.dump(vocab, f, ensure_ascii=False, indent=2)
    with open("intents.json", "w", encoding="utf-8") as f:
        json.dump(config.INTENTS, f, ensure_ascii=False, indent=2)
    vocab_size = len(vocab)
    print(f"词汇表大小: {vocab_size}")

    # 创建 DataLoader
    print("正在创建DataLoader...")
    train_dataset = IntentDataset(train_raw, vocab, config.MAX_LEN)
    val_dataset = IntentDataset(val_raw, vocab, config.MAX_LEN)
    train_loader = DataLoader(train_dataset, batch_size=config.BATCH_SIZE, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=config.BATCH_SIZE)

    # 初始化模型
    print("正在初始化AI模型...")
    model = AIIntentClassifier(vocab_size, embedding_dim=64, max_len=config.MAX_LEN, num_labels=config.NUM_LABELS).to(config.DEVICE)
    optimizer = torch.optim.Adam(model.parameters(), lr=config.LEARNING_RATE)
    criterion = nn.BCEWithLogitsLoss()

    # 训练
    print("\n开始训练AI模型...")
    print("AI模型正在学习识别不同意图的模式...")
    best_val_f1 = 0.0
    for epoch in range(1, config.EPOCHS+1):
        train_loss = train_epoch(model, train_loader, optimizer, criterion)
        val_loss, val_acc, val_f1 = evaluate(model, val_loader, criterion)
        print(f"Epoch {epoch:2d} | Train Loss: {train_loss:.4f} | Val Loss: {val_loss:.4f} | Val Acc: {val_acc:.4f} | Val F1: {val_f1:.4f}")

        if val_f1 > best_val_f1:
            best_val_f1 = val_f1
            torch.save(model.state_dict(), "best_model.pt")
            print("  -> 保存最佳AI模型")

    # 加载最佳模型
    print("正在加载最佳AI模型...")
    model.load_state_dict(torch.load("best_model.pt"))
    model.eval()

    # 测试关键句子
    print("\n" + "="*60)
    print("测试关键句子（AI模型预测）")
    test_sentences = [
        "我好热", "太热了", "好冷", "太冷了", "开灯", "查环境",
        "关灯顺便开制冷", "我好冷啊", "热死了", "冷死了", "有点热", "有点冷",
        "开暖气", "开冷气", "把温度调低一点", "调到26度", "温度调高一点",
        "把温度调到20度", "温度低一点", "调到28度", "30分钟后关灯",
        "2小时后开空调", "8点开灯", "切换到睡眠模式", "设置为自动模式",
        "把风速调到高速", "风速调到中速", "风速低一点",
        "开除湿", "打开送风", "十分钟后打开送风", "湿度太高打开除湿",
        "风速调成中并且开制热", "风俗调成钟并且开直热", "把风速调膏",
        "开空调并把温度调到24度", "关灯然后关空调", "打开客厅灯"
    ]

    for text in test_sentences:
        active, probs, params = predict(text, model, vocab, config.MAX_LEN)
        explanation = ai_explanation(text, probs, params)
        print(f"\n文本: {text}")
        print(f"  预测意图: {active if active else '无意图'}")
        print(f"  AI解释: {explanation}")
        print(f"  概率: {dict(zip(config.INTENTS, [round(prob, 3) for prob in probs]))}%")

    # 进入交互模式
    print("\n" + "="*60)
    interactive = input("是否进入AI交互测试模式？(y/n): ").strip().lower()
    if interactive == 'y':
        while True:
            text = input("\n请输入句子 (输入 quit 退出): ").strip()
            if text.lower() == 'quit':
                break
            if not text:
                continue
            start_time = time.time()
            active, probs, params = predict(text, model, vocab, config.MAX_LEN)
            inference_time = (time.time() - start_time) * 1000
            explanation = ai_explanation(text, probs, params)
            print(f"预测意图: {active if active else '无意图'}")
            print(f"AI解释: {explanation}")
            print(f"概率: {dict(zip(config.INTENTS, [round(prob, 3) for prob in probs]))}%")
            print(f"推理时间: {inference_time:.2f}ms")

# ==============================================
# 【仅在这里加了全局导出代码，其余100%未动！】
# 定义全局变量，供app.py导入
vocab = None
model = None
data_manager = DataManager()

def load_global_resources():
    global vocab, model
    # 优先加载训练时保存的词表，避免训练和推理时的字符索引不一致。
    vocab_path = Path("vocab.json")
    if vocab_path.exists():
        with vocab_path.open("r", encoding="utf-8") as f:
            vocab = json.load(f)
    else:
        train_raw, _ = data_manager.generate_dataset()
        all_texts = [item["text"] for item in train_raw]
        vocab = data_manager.build_vocab(all_texts)
    # 初始化模型
    model = AIIntentClassifier(len(vocab), embedding_dim=64, max_len=config.MAX_LEN, num_labels=config.NUM_LABELS).to(config.DEVICE)
    # 加载预训练权重
    try:
        model.load_state_dict(torch.load("best_model.pt", map_location=config.DEVICE))
        model.eval()
        print("全局模型/vocab加载完成")
    except Exception as e:
        print(f"未找到预训练模型，请先运行ai.py训练一次: {e}")

if __name__ == "__main__":
    main()
else:
    # 模块被app.py导入时，自动加载全局资源
    load_global_resources()
