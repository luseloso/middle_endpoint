import json
import time
import uuid
import requests
import google.auth
from google.auth.transport.requests import Request as AuthRequest
from flask import Flask, request, jsonify
from config import Config

app = Flask(__name__)

class AuthService:
    @staticmethod
    def get_access_token() -> str | None:
        try:
            credentials, _ = google.auth.default()
            credentials.refresh(AuthRequest())
            return credentials.token
        except Exception as e:
            print(f"Error getting access token: {e}")
            return None

@app.before_request
def check_shared_secret():
    # Allow OPTIONS requests for CORS if needed, though Flask-CORS isn't used here explicitly yet.
    if request.method == 'OPTIONS':
        return
        
    secret = Config.SHARED_SECRET
    if secret:
        auth_header = request.headers.get('X-Shared-Secret')
        if not auth_header or auth_header != secret:
            return jsonify({"error": "Unauthorized"}), 401

@app.route('/answer', methods=['POST'])
def get_answer():
    data = request.json
    query_text = data.get('query')
    session_val = data.get('session')
    preamble = data.get('preamble')
    reasoning_engine_id = data.get('reasoning_engine')

    if not query_text:
        return jsonify({"error": "Query is required"}), 400

    access_token = AuthService.get_access_token()
    if not access_token:
        return jsonify({"error": "Failed to authenticate"}), 500

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # REASONING ENGINE LOGIC
    if reasoning_engine_id:
        session_id = session_val
        if not session_id:
            # Default to empty string for RE if not provided, ensuring agent works
            session_id = ""

        re_project_id = Config.PROJECT_ID
        re_location = Config.REASONING_ENGINE_LOCATION
        
        api_url = f"https://{re_location}-aiplatform.googleapis.com/v1/projects/{re_project_id}/locations/{re_location}/reasoningEngines/{reasoning_engine_id}:streamQuery?alt=sse"

        payload = {
            "class_method": "async_stream_query",
            "input": {
                "message": query_text,
                "session_id": session_id,
                "user_id": "test"
            }
        }

        try:
            response = requests.post(api_url, headers=headers, json=payload, stream=True)
            response.raise_for_status()
            
            accumulated_text = []
            
            for line in response.iter_lines():
                if line:
                    decoded_line = line.decode('utf-8').strip()
                    if not decoded_line:
                        continue
                    
                    json_str = decoded_line
                    if decoded_line.startswith('data: '):
                        json_str = decoded_line[6:] # Strip "data: "
                    
                    try:
                        data_chunk = json.loads(json_str)
                        content = data_chunk.get('content', {})
                        parts = content.get('parts', [])
                        for part in parts:
                            if 'text' in part:
                                accumulated_text.append(part['text'])
                    except json.JSONDecodeError:
                        pass

            full_answer = "".join(accumulated_text)
            if not full_answer:
                full_answer = "No text returned from stream."
            
            return jsonify({
                "answer": full_answer,
                "citations": [],
                "references": [],
                "session": session_id,
                "related_questions": []
            })

        except requests.exceptions.RequestException as e:
            error_msg = str(e)
            if e.response is not None:
                error_msg = e.response.text
            return jsonify({"error": f"Reasoning Engine Request failed: {error_msg}"}), 500

    # DISCOVERY ENGINE LOGIC (Default)
    session_name = session_val
    project_id = Config.PROJECT_ID
    location = Config.LOCATION
    collection_id = Config.COLLECTION_ID
    engine_id = Config.ENGINE_ID
    serving_config_id = Config.SERVING_CONFIG_ID
    
    if not session_name:
        try:
            session_api_url = f"https://discoveryengine.googleapis.com/v1/projects/{project_id}/locations/{location}/collections/{collection_id}/engines/{engine_id}/sessions"
            session_payload = {"userPseudoId": "test-user"}
            session_resp = requests.post(session_api_url, headers=headers, json=session_payload)
            if session_resp.status_code == 200:
                session_name = session_resp.json().get("name")
            else:
                print(f"Failed to create session: {session_resp.text}")
        except Exception as e:
            print(f"Session creation exception: {e}")
    
    # Ensure session_name is used in payload if available
    
    api_url = f"https://discoveryengine.googleapis.com/v1/projects/{project_id}/locations/{location}/collections/{collection_id}/engines/{engine_id}/servingConfigs/{serving_config_id}:answer"
    
    answer_gen_spec = {
        "includeCitations": True,
        "answerLanguageCode": "en"
    }
    if preamble:
        answer_gen_spec["promptSpec"] = {"preamble": preamble}

    payload = {
        "query": {"text": query_text},
        "session": session_name,
        #"relatedQuestionsSpec": {"enable": False},
        "answerGenerationSpec": answer_gen_spec
    }

    try:
        start_time = time.time()
        response = requests.post(api_url, headers=headers, json=payload)
        elapsed_time = time.time() - start_time
        print(f"Discovery Engine Answer API call took: {elapsed_time:.4f} seconds")
        response.raise_for_status()
        result = response.json()
        
        answer_data = result.get("answer", {})
        
        session_val_resp = result.get("session")
        session_name_out = None
        if isinstance(session_val_resp, dict):
             session_name_out = session_val_resp.get("name")
        else:
             session_name_out = session_val_resp
             
        if not session_name_out:
             session_info = result.get("sessionInfo", {})
             session_name_out = session_info.get("name")
        
        output = {
            "answer": answer_data.get("answerText", "No answer found."),
            "citations": answer_data.get("citations", []),
            "references": answer_data.get("references", []),
            "session": session_name_out,
            "related_questions": answer_data.get("relatedQuestions", [])
        }
        
        return jsonify(output)

    except requests.exceptions.RequestException as e:
        error_msg = str(e)
        if e.response is not None:
             error_msg = e.response.text
        return jsonify({"error": f"API request failed: {error_msg}"}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8081)
