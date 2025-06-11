""" import streamlit as st
import boto3
import time
import json

# --- Config ---
BUCKET = "billing-pipeline-bucket"
REGION = "us-east-1"
PROFILE = "eit"
MODEL_ID = "anthropic.claude-3-5-sonnet-20240620-v1:0"
MAX_TOKENS = 1000
TEMPERATURE = 0.5
TOP_P = 0.7
TOP_K = 0

# --- AWS Session ---
session = boto3.Session(profile_name=PROFILE)
s3 = session.client("s3", region_name=REGION)
textract = session.client("textract", region_name=REGION)
bedrock = session.client("bedrock-runtime", region_name=REGION)

# --- Upload Helper ---
def upload_to_s3(file, key):
    try:
        s3.upload_fileobj(file, BUCKET, key)
        return True, key
    except Exception as e:
        return False, str(e)

# --- Textract Helper ---
def analyze_with_textract(bucket, file_key):
    response = textract.start_document_analysis(
        DocumentLocation={'S3Object': {'Bucket': bucket, 'Name': file_key}},
        FeatureTypes=['FORMS'] 
    )
    job_id = response['JobId']
    while True:
        result = textract.get_document_analysis(JobId=job_id)
        status = result['JobStatus']
        if status in ['SUCCEEDED', 'FAILED']:
            break
        time.sleep(5)

    if status == 'FAILED':
        return None, "Textract job failed"

    blocks = []
    next_token = None
    while True:
        if next_token:
            result = textract.get_document_analysis(JobId=job_id, NextToken=next_token)
        else:
            result = textract.get_document_analysis(JobId=job_id)
        blocks.extend(result['Blocks'])
        next_token = result.get('NextToken')
        if not next_token:
            break
    return blocks, None

# --- Extract Form Data ---
def extract_forms(blocks):
    block_map = {b['Id']: b for b in blocks}
    forms = []

    for block in blocks:
        if block['BlockType'] == 'KEY_VALUE_SET' and 'KEY' in block.get('EntityTypes', []):
            key_text = ""
            value_text = ""
            for rel in block.get('Relationships', []):
                if rel['Type'] == 'CHILD':
                    for cid in rel['Ids']:
                        word = block_map.get(cid)
                        if word and word['BlockType'] == 'WORD':
                            key_text += word['Text'] + ' '
                if rel['Type'] == 'VALUE':
                    for vid in rel['Ids']:
                        value_block = block_map.get(vid)
                        for vrel in value_block.get('Relationships', []):
                            if vrel['Type'] == 'CHILD':
                                for cid in vrel['Ids']:
                                    word = block_map.get(cid)
                                    if word and word['BlockType'] == 'WORD':
                                        value_text += word['Text'] + ' '
            forms.append((key_text.strip(), value_text.strip()))
    return forms

# --- Claude Prompt Builder ---
def build_prompt(forms):
    prompt = "Human: You are an AWS cost optimization expert. Based on the bill data below:\n\n"
    prompt += "- Identify the top 3 to 5 of the highest-cost services.\n"
    prompt += "- Recommend precise AWS cost-saving actions \n"
    prompt += "- Skip repeating the bill and avoid generic advice.\n\n"
    prompt += "Bill Data:\n"

    for k, v in forms:
        prompt += f"{k}: {v}\n"

    prompt += "\nAssistant:"
    return prompt

# --- Claude Bedrock Call ---
def ask_claude(prompt):
    body = {
        "prompt": prompt,
        "max_tokens_to_sample": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "top_k": TOP_K,
        "top_p": TOP_P,
        "stop_sequences": ["\n\nHuman:"]
    }
    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        contentType='application/json',
        accept='application/json',
        body=json.dumps(body).encode('utf-8')
    )
    result_json = json.loads(response['body'].read().decode('utf-8'))
    return result_json.get("completion", "❌ No response from Claude")

# --- Streamlit UI ---
st.set_page_config(page_title="AWS Bill Analyzer", layout="centered")
st.title("📃 AWS Bill Analyzer")

uploaded_file = st.file_uploader("Upload your AWS bill (PDF/JPG/PNG)", type=["pdf", "jpg", "jpeg", "png"])
file_key = None
textract_blocks = None
extracted_forms = None

if uploaded_file:
    st.success(f"Selected: {uploaded_file.name}")

    if st.button("1️⃣ Upload to S3"):
        with st.spinner("Uploading to S3..."):
            success, result = upload_to_s3(uploaded_file, f"uploads/{uploaded_file.name}")
        if success:
            st.session_state['file_key'] = result
            st.success("✅ File uploaded!")
        else:
            st.error(f"❌ Upload failed: {result}")

if 'file_key' in st.session_state and st.button("2️⃣ Analyze with Textract"):
    with st.spinner("Running Textract..."):
        blocks, error = analyze_with_textract(BUCKET, st.session_state['file_key'])
    if error:
        st.error(error)
    else:
        st.session_state['blocks'] = blocks
        st.session_state['forms'] = extract_forms(blocks)
        st.success("✅ Textract completed!")

        st.subheader("🔑 Extracted Key-Value Pairs")
        for k, v in st.session_state['forms']:
            st.text(f"{k}: {v}")

if 'forms' in st.session_state and st.button("3️⃣ Get Claude Optimization Suggestions"):
    with st.spinner("Claude is thinking..."):
        prompt = build_prompt(st.session_state['forms'])
        suggestion = ask_claude(prompt)
        st.session_state['claude_suggestion'] = suggestion

    st.subheader("🤖 Claude’s Cost Optimization Suggestions")
    st.markdown(f"```\n{suggestion.strip()}\n```)\n")

if 'claude_suggestion' in st.session_state:
    user_query = st.text_input("💬 Have a follow-up question?")
    if user_query and st.button("Ask Claude"):
        context = (
            "You are an AWS cost optimization expert. Based on this AWS bill (key-value data),"
            " and your earlier suggestions (provided below), answer the user's new question only."
            " Do not repeat earlier suggestions or the bill again.\n\n"
            "Bill Data:\n"
        )
        for k, v in st.session_state['forms']:
            context += f"{k}: {v}\n"

        context += f"\nYour Previous Suggestions:\n{st.session_state['claude_suggestion'].strip()}\n\n"
        full_prompt = f"Human: {context}\nUser's Question: {user_query}\n\nAssistant:"

        with st.spinner("Claude is answering..."):
            followup = ask_claude(full_prompt)

        st.markdown("🧠 Claude's Response to Your Question:")
        st.markdown(f"```\n{followup.strip()}\n```") 
 """

import streamlit as st
import boto3
import time
import json

# --- Config ---
BUCKET = "billing-pipeline-bucket"
REGION = "us-east-1"
PROFILE = "eit"
MODEL_ID = "anthropic.claude-3-5-sonnet-20240620-v1:0"
MAX_TOKENS = 1000
TEMPERATURE = 0.5
TOP_P = 0.7

# --- AWS Session ---
session = boto3.Session(profile_name=PROFILE)
s3 = session.client("s3", region_name=REGION)
textract = session.client("textract", region_name=REGION)
bedrock = session.client("bedrock-runtime", region_name=REGION)

# --- Upload Helper ---
def upload_to_s3(file, key):
    try:
        s3.upload_fileobj(file, BUCKET, key)
        return True, key
    except Exception as e:
        return False, str(e)

# --- Textract Helper ---
def analyze_with_textract(bucket, file_key):
    response = textract.start_document_analysis(
        DocumentLocation={'S3Object': {'Bucket': bucket, 'Name': file_key}},
        FeatureTypes=['FORMS'] 
    )
    job_id = response['JobId']
    while True:
        result = textract.get_document_analysis(JobId=job_id)
        status = result['JobStatus']
        if status in ['SUCCEEDED', 'FAILED']:
            break
        time.sleep(5)

    if status == 'FAILED':
        return None, "Textract job failed"

    blocks = []
    next_token = None
    while True:
        if next_token:
            result = textract.get_document_analysis(JobId=job_id, NextToken=next_token)
        else:
            result = textract.get_document_analysis(JobId=job_id)
        blocks.extend(result['Blocks'])
        next_token = result.get('NextToken')
        if not next_token:
            break
    return blocks, None

# --- Extract Form Data ---
def extract_forms(blocks):
    block_map = {b['Id']: b for b in blocks}
    forms = []

    for block in blocks:
        if block['BlockType'] == 'KEY_VALUE_SET' and 'KEY' in block.get('EntityTypes', []):
            key_text = ""
            value_text = ""
            for rel in block.get('Relationships', []):
                if rel['Type'] == 'CHILD':
                    for cid in rel['Ids']:
                        word = block_map.get(cid)
                        if word and word['BlockType'] == 'WORD':
                            key_text += word['Text'] + ' '
                if rel['Type'] == 'VALUE':
                    for vid in rel['Ids']:
                        value_block = block_map.get(vid)
                        for vrel in value_block.get('Relationships', []):
                            if vrel['Type'] == 'CHILD':
                                for cid in vrel['Ids']:
                                    word = block_map.get(cid)
                                    if word and word['BlockType'] == 'WORD':
                                        value_text += word['Text'] + ' '
            forms.append((key_text.strip(), value_text.strip()))
    return forms

# --- Claude Prompt Builder ---
def build_prompt(forms):
    messages = [
        {
            "role": "user",
            "content": (
                "You are an AWS cost optimization expert. Based on the bill data below:\n"
                "- Identify the top 3 to 5 highest-cost services.\n"
                "- Recommend precise AWS cost-saving actions.\n"
                "- Skip repeating the bill and avoid generic advice.\n\n"
                "Bill Data:\n" +
                "\n".join(f"{k}: {v}" for k, v in forms)
            )
        }
    ]
    return messages

# --- Claude Bedrock Call (Messages API) ---
def ask_claude(messages):
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "messages": messages,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "top_p": TOP_P
    }
    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        contentType='application/json',
        accept='application/json',
        body=json.dumps(body).encode('utf-8')
    )
    result_json = json.loads(response['body'].read().decode('utf-8'))
    return result_json["content"][0]["text"] if result_json.get("content") else "❌ No response from Claude"

# --- Streamlit UI ---
st.set_page_config(page_title="AWS Bill Analyzer", layout="centered")
st.title("📃 AWS Bill Analyzer")

uploaded_file = st.file_uploader("Upload your AWS bill (PDF/JPG/PNG)", type=["pdf", "jpg", "jpeg", "png"])
file_key = None
textract_blocks = None
extracted_forms = None

if uploaded_file:
    st.success(f"Selected: {uploaded_file.name}")

    if st.button("1️⃣ Upload to S3"):
        with st.spinner("Uploading to S3..."):
            success, result = upload_to_s3(uploaded_file, f"uploads/{uploaded_file.name}")
        if success:
            st.session_state['file_key'] = result
            st.success("✅ File uploaded!")
        else:
            st.error(f"❌ Upload failed: {result}")

if 'file_key' in st.session_state and st.button("2️⃣ Analyze with Textract"):
    with st.spinner("Running Textract..."):
        blocks, error = analyze_with_textract(BUCKET, st.session_state['file_key'])
    if error:
        st.error(error)
    else:
        st.session_state['blocks'] = blocks
        st.session_state['forms'] = extract_forms(blocks)
        st.success("✅ Textract completed!")

        st.subheader("🔑 Extracted Key-Value Pairs")
        for k, v in st.session_state['forms']:
            st.text(f"{k}: {v}")

if 'forms' in st.session_state and st.button("3️⃣ Get Claude Optimization Suggestions"):
    with st.spinner("Claude is thinking..."):
        messages = build_prompt(st.session_state['forms'])
        suggestion = ask_claude(messages)
        st.session_state['claude_suggestion'] = suggestion

    st.subheader("🤖 Claude’s Cost Optimization Suggestions")
    st.markdown(f"```{suggestion.strip()}```")

if 'claude_suggestion' in st.session_state:
    user_query = st.text_input("💬 Have a follow-up question?")
    if user_query and st.button("Ask Claude"):
        context = (
            "You are an AWS cost optimization expert. Based on this AWS bill (key-value data),"
            " and your earlier suggestions (provided below), answer the user's new question only."
            " Do not repeat earlier suggestions or the bill again.\n\n"
            "Bill Data:\n"
        )
        for k, v in st.session_state['forms']:
            context += f"{k}: {v}\n"

        context += f"\nYour Previous Suggestions:\n{st.session_state['claude_suggestion'].strip()}\n\n"
        user_message = f"{context}\nUser's Question: {user_query}"

        with st.spinner("Claude is answering..."):
            followup = ask_claude([{ "role": "user", "content": user_message }])

        st.markdown("🧠 Claude's Response to Your Question:")
        st.markdown(f"```{followup.strip()}```") 