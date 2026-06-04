import os
from flask import Flask, render_template

template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'templates'))
app = Flask(__name__, template_folder=template_dir)

@app.route('/')
def dashboard():
    return render_template('dashboard.html')

@app.route('/history')
def history():
    return render_template('history.html')

@app.route('/camera')
def camera_page():
    return render_template('camera.html')

@app.route('/google-photos')
def google_photos_page():
    return render_template('google_photos.html')

@app.route('/live-video')
def live_video():
    return render_template('live_video.html')

@app.route('/folders')
def folders_page():
    return render_template('folders.html')

@app.route('/settings')
def settings_page():
    return render_template('settings.html')

@app.route('/system')
def system_status_page():
    return render_template('status.html')

@app.route('/terminal')
def terminal_page():
    return render_template('terminal.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002)
