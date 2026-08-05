import requests


def checkin(user_id):
    url = "http://localhost:5000/checkin"
    data = {"user_id": user_id}
    response = requests.post(url, json=data)
    if response.status_code == 200:
        return response.json()
    else:
        return None


if __name__ == "__main__":
    user_id = "user123"
    result = checkin(user_id)
    print(result)
