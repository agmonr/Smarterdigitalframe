import os
import json
import configparser
from flask import Flask, render_template, send_from_directory
import common

app = Flask(__name__)

# Load configuration
IMAGE_DIR = common.get_image_dir()

def get_images():
    return common.get_images(IMAGE_DIR)

@app.route('/')
def index():
    images = get_images()
    history = common.get_history(limit=10)
    return render_template('index.html', images=images, history=history)

@app.route('/image/<filename>')
def serve_image(filename):
    return send_from_directory(IMAGE_DIR, filename)

if __name__ == '__main__':
    # Listen on all interfaces so it's accessible from the network
    app.run(host='0.0.0.0', port=5000)
