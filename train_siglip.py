import os
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, random_split, WeightedRandomSampler
from torchvision.datasets import ImageFolder
from transformers import AutoProcessor, SiglipVisionModel
import numpy as np
import json

# --- Config Initialization ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Loading parameters centrally from the global config file
ROOT_DIR = os.path.dirname(BASE_DIR) if "Regime_Filter" in BASE_DIR else BASE_DIR
CONFIG_PATH = os.path.join(ROOT_DIR, "system_config.json")
DATASET_DIR = os.path.join(BASE_DIR, "Regime_Filter/dataset")

with open(CONFIG_PATH, 'r') as f:
    config = json.load(f)

vt_config = config['ml_pipeline']['vision_transformer']
train_params = vt_config['training_params']

# Dynamically load parameters
BATCH_SIZE = train_params['batch_size']
EPOCHS = train_params['epochs']
LEARNING_RATE = train_params['learning_rate']
TRAIN_SPLIT = train_params['train_test_split']
MODEL_SAVE_PATH = os.path.join(ROOT_DIR, vt_config['model_save_path'])

# Ensure the output directory exists
os.makedirs(os.path.dirname(MODEL_SAVE_PATH), exist_ok=True)

class SiglipClassifier(nn.Module):
    def __init__(self, num_classes):
        super().__init__()
        self.vision_model = SiglipVisionModel.from_pretrained("google/siglip-base-patch16-224")
        
        # 1. Freeze the entire brain to protect the foundational visual geometry
        for param in self.vision_model.parameters():
            param.requires_grad = False
            
        # 2. Unfreeze ONLY the last 4 layers so it can learn candlestick meaning
        for layer in self.vision_model.vision_model.encoder.layers[-4:]:
            for param in layer.parameters():
                param.requires_grad = True

        # 3. Unfreeze the final pooling head (SigLIP uses .head instead of .pooler)
        for param in self.vision_model.vision_model.head.parameters():
            param.requires_grad = True
            
        # 4. Unfreeze the post-layernorm to ensure clean signal flow
        for param in self.vision_model.vision_model.post_layernorm.parameters():
            param.requires_grad = True
            
        self.classifier = nn.Sequential(
            nn.Dropout(0.3),  
            nn.Linear(self.vision_model.config.hidden_size, num_classes)
        )

    def forward(self, pixel_values):
        outputs = self.vision_model(pixel_values=pixel_values)
        pooled_output = outputs.pooler_output 
        logits = self.classifier(pooled_output)
        return logits

def train_model():
    print("🚀 Initializing SigLIP Training Pipeline (Partial Freeze + Balanced Sampler)...")
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"🖥️ Using Device: {device}")

    processor = AutoProcessor.from_pretrained("google/siglip-base-patch16-224")
    
    full_dataset = ImageFolder(root=DATASET_DIR)
    
    classes_dict = vt_config['classes']
    valid_classes = [classes_dict[key] for key in sorted(classes_dict.keys())]
    
    class_to_idx = {k: v for k, v in full_dataset.class_to_idx.items() if k in valid_classes}
    print(f"📁 Found Classes: {class_to_idx}")

    def collate_fn(batch):
        images, labels = zip(*batch)
        inputs = processor(images=list(images), return_tensors="pt")
        inputs['labels'] = torch.tensor(labels)
        return inputs

    # Train/Val Split
    train_size = int(TRAIN_SPLIT * len(full_dataset))
    val_size = len(full_dataset) - train_size
    train_dataset, val_dataset = random_split(full_dataset, [train_size, val_size])
    
    # --- The "Balanced Diet" Sampler Fix ---
    print("⚖️ Constructing Weighted Sampler to balance batches...")
    
    # Extract the true labels for just the training subset
    train_targets = [full_dataset.targets[i] for i in train_dataset.indices]
    class_counts = np.bincount(train_targets)
    
    print(f"📊 Training Subset Distribution: {class_counts}")
    
    # Weight is the inverse of the frequency. Rare classes get massive draw probability.
    class_weights_arr = 1.0 / class_counts
    sample_weights = [class_weights_arr[t] for t in train_targets]

    # Create the sampler. We draw exactly as many samples as we have in the train set.
    sampler = WeightedRandomSampler(weights=sample_weights, num_samples=len(sample_weights), replacement=True)

    # Note: shuffle=True is REMOVED because the sampler handles the randomization
    train_loader = DataLoader(train_dataset, batch_size=BATCH_SIZE, sampler=sampler, collate_fn=collate_fn)
    # Validation loader doesn't need balancing; we want to test on the real-world distribution
    val_loader = DataLoader(val_dataset, batch_size=BATCH_SIZE, shuffle=False, collate_fn=collate_fn)

    model = SiglipClassifier(num_classes=len(valid_classes)).to(device)
    
    # Clean CrossEntropyLoss (no heavy weight penalties to cause explosions)
    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.AdamW(model.parameters(), lr=LEARNING_RATE, weight_decay=0.01)

    print("\n🔥 Starting Training Forge...")
    best_val_acc = 0.0

    for epoch in range(EPOCHS):
        model.train()
        total_loss = 0.0
        correct_train = 0
        total_train = 0

        for batch in train_loader:
            pixel_values = batch['pixel_values'].to(device)
            labels = batch['labels'].to(device)

            optimizer.zero_grad()
            outputs = model(pixel_values)
            loss = criterion(outputs, labels)
            
            loss.backward()
            optimizer.step()

            total_loss += loss.item()
            _, predicted = torch.max(outputs.data, 1)
            total_train += labels.size(0)
            correct_train += (predicted == labels).sum().item()

        train_acc = 100 * correct_train / total_train

        model.eval()
        correct_val = 0
        total_val = 0
        with torch.no_grad():
            for batch in val_loader:
                pixel_values = batch['pixel_values'].to(device)
                labels = batch['labels'].to(device)
                
                outputs = model(pixel_values)
                _, predicted = torch.max(outputs.data, 1)
                
                total_val += labels.size(0)
                correct_val += (predicted == labels).sum().item()

        val_acc = 100 * correct_val / total_val
        print(f"Epoch [{epoch+1}/{EPOCHS}] | Train Loss: {total_loss/len(train_loader):.4f} | Train Acc: {train_acc:.2f}% | Val Acc: {val_acc:.2f}%")

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            torch.save(model.state_dict(), MODEL_SAVE_PATH)
            print(f"   💾 New Best Model Saved! (Accuracy: {best_val_acc:.2f}%)")

    print(f"\n✅ Training Complete. Best Validation Accuracy: {best_val_acc:.2f}%")
    print(f"Your model is saved at: {MODEL_SAVE_PATH}")

if __name__ == "__main__":
    train_model()