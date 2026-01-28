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
    filter_str = data.get('filter')
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
        "answerGenerationSpec": answer_gen_spec,
    }

    if filter_str:
        payload["searchSpec"] = {
            "searchParams": {
                "filter": filter_str
            }
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

@app.route('/answer2', methods=['POST'])
def get_answer2():
    """
    Endpoint using StreamAssist (streamAnswer) to get answers.
    Ignores Reasoning Engine logic.
    """
    data = request.json
    query_text = data.get('query')
    session_val = data.get('session')
    preamble = data.get('preamble')
    # reasoning_engine_id ignored for this endpoint

    if not query_text:
        return jsonify({"error": "Query is required"}), 400

    access_token = AuthService.get_access_token()
    if not access_token:
        return jsonify({"error": "Failed to authenticate"}), 500

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # DISCOVERY ENGINE LOGIC (StreamAnswer)
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
    
    # Use v1beta for streamAnswer as it is often the available version for streaming methods in REST
    # Note: 'StreamAssist' often refers to the streamAnswer capability.
    api_url = f"https://discoveryengine.googleapis.com/v1beta/projects/{project_id}/locations/{location}/collections/{collection_id}/engines/{engine_id}/servingConfigs/{serving_config_id}:streamAnswer"
    
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
        # Stream=True for streaming response
        response = requests.post(api_url, headers=headers, json=payload, stream=True)
        # We don't verify status code immediately here as it might be streaming error 
        if response.status_code >= 400:
             # Try to read error content
             error_content = response.text
             return jsonify({"error": f"StreamAnswer request failed: {error_content}"}), response.status_code

        accumulated_text = ""
        final_answer_data = {}
        
        # Parse full response payload
        # We wait for the complete payload as per requirements to handle potential framing issues
        full_response_text = response.text
        
        decoder = json.JSONDecoder()
        pos = 0
        while pos < len(full_response_text):
            # Skip whitespace
            while pos < len(full_response_text) and full_response_text[pos].isspace():
                pos += 1
            if pos >= len(full_response_text):
                break
                
            try:
                chunk_data, end = decoder.raw_decode(full_response_text, idx=pos)
                pos = end
                
                # Handle potential list if API returns a JSON array
                items = [chunk_data] if isinstance(chunk_data, dict) else chunk_data
                if not isinstance(items, list):
                    continue

                for item in items:
                    if not isinstance(item, dict):
                        continue

                    if "answer" in item:
                        ans_obj = item["answer"]
                        text_chunk = ans_obj.get("answerText", "")
                        
                        if text_chunk:
                            accumulated_text = text_chunk
                        
                        if "citations" in ans_obj:
                            final_answer_data["citations"] = ans_obj["citations"]
                        if "references" in ans_obj:
                            final_answer_data["references"] = ans_obj["references"]
                        if "relatedQuestions" in ans_obj:
                            final_answer_data["relatedQuestions"] = ans_obj["relatedQuestions"]

                    if "session" in item:
                         final_answer_data["session"] = item["session"]
            
            except json.JSONDecodeError:
                print(f"Failed to decode JSON chunk at pos {pos}")
                break

        elapsed_time = time.time() - start_time
        print(f"Discovery Engine StreamAnswer API call took: {elapsed_time:.4f} seconds")

        # Fallback if no text
        if not accumulated_text:
             accumulated_text = "No answer found."

        # Session name normalization
        session_val_resp = final_answer_data.get("session")
        session_name_out = None
        if isinstance(session_val_resp, dict):
             session_name_out = session_val_resp.get("name")
        else:
             session_name_out = session_val_resp
             
        # If session not in response, use the input session_name
        if not session_name_out:
             session_name_out = session_name

        output = {
            "answer": accumulated_text,
            "citations": final_answer_data.get("citations", []),
            "references": final_answer_data.get("references", []),
            "session": session_name_out,
            "related_questions": final_answer_data.get("relatedQuestions", [])
        }
        
        return jsonify(output)

    except requests.exceptions.RequestException as e:
        error_msg = str(e)
        return jsonify({"error": f"API request failed: {error_msg}"}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8081)
