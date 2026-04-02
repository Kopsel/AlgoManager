import os
import shutil
import tkinter as tk
from PIL import Image, ImageTk
import json

# --- Config Initialization ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Tell Python to go UP one folder level to find your global config
ROOT_DIR = os.path.dirname(BASE_DIR) 
CONFIG_PATH = os.path.join(ROOT_DIR, "system_config.json")

# Keep the dataset operations inside the Regime_Filter folder
DATASET_DIR = os.path.join(BASE_DIR, "dataset")
UNLABELED_DIR = os.path.join(DATASET_DIR, "unlabeled")

with open(CONFIG_PATH, 'r') as f:
    config = json.load(f)

classes_dict = config['ml_pipeline']['vision_transformer']['classes']

# Map keys to the JSON folder names
CATEGORIES = {
    '1': classes_dict["0"],  # "longs_only"
    '2': classes_dict["1"],  # "shorts_only"
    '3': classes_dict["2"],  # "scalp_both"
    '<space>': "trash"
}

class LabelerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("SigLIP Regime Labeler")
        
        for folder in CATEGORIES.values():
            os.makedirs(os.path.join(DATASET_DIR, folder), exist_ok=True)
            
        self.images = [f for f in os.listdir(UNLABELED_DIR) if f.endswith('.png')]
        self.total_images = len(self.images)
        self.current_index = 0

        if self.total_images == 0:
            tk.Label(root, text="No images found in 'unlabeled' folder!", font=("Arial", 20)).pack()
            return

        self.info_label = tk.Label(root, text="", font=("Arial", 14))
        self.info_label.pack(pady=10)

        self.image_label = tk.Label(root)
        self.image_label.pack()
        
        instructions = f"[ 1 ] {CATEGORIES['1'].upper()}   |   [ 2 ] {CATEGORIES['2'].upper()}   |   [ 3 ] {CATEGORIES['3'].upper()}   |   [ SPACE ] TRASH"
        tk.Label(root, text=instructions, font=("Arial", 14, "bold"), fg="dodgerblue").pack(pady=10)

        for key in ['1', '2', '3']:
            self.root.bind(key, self.handle_keypress)
        self.root.bind('<space>', self.handle_keypress)

        self.load_image()

    def load_image(self):
        if self.current_index >= self.total_images:
            self.info_label.config(text="🎉 All images labeled! You are ready to train.")
            self.image_label.config(image='')
            return

        filename = self.images[self.current_index]
        self.info_label.config(text=f"Image {self.current_index + 1} of {self.total_images} | {filename}")

        img_path = os.path.join(UNLABELED_DIR, filename)
        img = Image.open(img_path)
        img = img.resize((800, 800), Image.Resampling.LANCZOS) 
        
        self.tk_img = ImageTk.PhotoImage(img)
        self.image_label.config(image=self.tk_img)

    def handle_keypress(self, event):
        key = '<space>' if event.keysym == 'space' else event.char
            
        if key in CATEGORIES:
            filename = self.images[self.current_index]
            src_path = os.path.join(UNLABELED_DIR, filename)
            dst_path = os.path.join(DATASET_DIR, CATEGORIES[key], filename)
            
            shutil.move(src_path, dst_path)
            print(f"Moved {filename} -> {CATEGORIES[key]}")
            
            self.current_index += 1
            self.load_image()

if __name__ == "__main__":
    root = tk.Tk()
    app = LabelerApp(root)
    root.attributes('-topmost', True)
    root.update()
    root.attributes('-topmost', False)
    root.mainloop()