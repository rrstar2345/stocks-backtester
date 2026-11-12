import os
import json
from cryptography.fernet import Fernet
from fyers_apiv3 import fyersModel

CRED_FILE = ".fyers_secure_cred.enc"
KEY_FILE = ".fyers_key.key"
TOKEN_FILE = "access_token.txt"

def get_or_create_cipher():
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            key = f.read()
    else:
      key = Fernet.generate_key()
      with open(KEY_FILE, "wb") as f:
          f.write(key)
    return Fernet(key)

def save_credentials(client_id, secret_id, redirect_uri):
    cipher = get_or_create_cipher()
    data = json.dumps({
        "client_id": client_id,
        "secret_id": secret_id,
        "redirect_uri": redirect_uri
    })
    encrypted_data = cipher.encrypt(data.encode())
    with open(CRED_FILE, "wb") as f:
        f.write(encrypted_data)

def load_credentials():
    if not os.path.exists(CRED_FILE):
        return None
    cipher = get_or_create_cipher()
    with open(CRED_FILE, "rb") as f:
        encrypted_data = f.read()
    try:
        decrypted_data = cipher.decrypt(encrypted_data).decode()
        return json.loads(decrypted_data)
    except Exception:
        return None

def get_auth_url(client_id, redirect_uri):
    session = fyersModel.SessionModel(
        client_id=client_id,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code"
    )
    return session.generate_authcode()

def generate_access_token(client_id, secret_id, redirect_uri, auth_code):
    session = fyersModel.SessionModel(
        client_id=client_id,
        secret_key=secret_id,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code"
    )
    session.set_token(auth_code)
    response = session.generate_token()
    
    if "access_token" in response:
        access_token = response["access_token"]
        # Fyers API v3 requires client_id:access_token format for REST calls
        full_token = f"{client_id}:{access_token}"
        with open(TOKEN_FILE, "w") as f:
            f.write(full_token)
        return full_token
    raise Exception(f"Failed to generate access token: {response}")

def get_fyers_client():
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE, "r") as f:
        full_token = f.read().strip()
    
    if ":" not in full_token:
        return None
        
    client_id = full_token.split(":")[0]
    return fyersModel.FyersModel(client_id=client_id, token=full_token.split(":")[1], is_async=False)