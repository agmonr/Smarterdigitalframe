import socketio
sio = socketio.Client()

@sio.event
def connect():
    print("Connected to server")
    sio.disconnect()

try:
    sio.connect('http://127.0.0.1:5004', namespaces=['/pty'])
except Exception as e:
    print(f"Connection failed: {e}")
