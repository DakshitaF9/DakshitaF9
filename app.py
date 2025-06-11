import streamlit as st
import boto3
import time
import json

# --- Config ---
BUCKET = "billing-pipeline-bucket"
REGION = "us-east-1"
PROFILE = "eit"
MODEL_ID = "anthropic.claude-instant-v1"
MAX_TOKENS = 1000
TEMPERATURE = 0.7
TOP_P = 0.9
TOP_K = 250

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

# --- Extract Data ---
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

st.title("📃 AWS Bill Analyzer + Claude Suggestions")
uploaded_file = st.file_uploader("Upload your AWS bill (PDF/JPG/PNG)", type=["pdf", "jpg", "jpeg", "png"])

if uploaded_file:
    st.success(f"Selected: {uploaded_file.name}")
    if st.button("Upload and Get Cost Optimization Tips"):
        with st.spinner("Uploading..."):
            success, result = upload_to_s3(uploaded_file, f"uploads/{uploaded_file.name}")
        if not success:
            st.error(f"Upload failed: {result}")
        else:
            st.success("Uploaded to S3!")
            with st.spinner("Analyzing with Textract..."):
                blocks, error = analyze_with_textract(BUCKET, result)
            if error:
                st.error(error)
            else:
                forms = extract_forms(blocks)

                st.subheader("🔑 Key-Value Pairs")
                for k, v in forms:
                    st.text(f"{k}: {v}")

                st.subheader("🤖 Claude’s Cost Optimization Suggestions")
                with st.spinner("Thinking..."):
                    prompt = build_prompt(forms)
                    suggestion = ask_claude(prompt)
                st.success("Done!")
                st.markdown(f"```\n{suggestion.strip()}\n```")
