from flask import Flask, request
import uuid

app = Flask(__name__)

@app.route('/checkin', methods=['POST'])
def checkin():
    data = request.get_json()
    if not data or 'user_id' not in data:
        return {'error': 'Invalid request'}, 400
    user_id = data['user_id']
    return {'user_id': user_id, 'status': 'checked in', 'checkin_id': str(uuid.uuid4())}

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)