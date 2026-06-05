import os
import json
import configparser
from flask import Flask, render_template, send_from_directory

app = Flask(__name__)

# Load configuration
config = configparser.ConfigParser()
config.read('config.ini')
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = config.get('DEFAULT', 'ImageDir', fallback=os.path.join(PROJECT_ROOT, 'images/'))
HISTORY_FILE = 'history.json'

def get_images():
    valid_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.bmp')
    return [f for f in os.listdir(IMAGE_DIR)
            if f.lower().endswith(valid_extensions)]

def get_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r') as f:
            try:
                return json.load(f)[-10:] # Return last 10 entries
            except:
                return []
    return []

@app.route('/')
def index():
    images = get_images()
    history = get_history()
    return render_template('index.html', images=images, history=history)

@app.route('/image/<filename>')
def serve_image(filename):
    return send_from_directory(IMAGE_DIR, filename)

if __name__ == '__main__':
    # Listen on all interfaces so it's accessible from the network
    app.run(host='0.0.0.0', port=5000)
