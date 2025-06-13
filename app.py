""" import streamlit as st
import boto3
import time
import json
from boto3.dynamodb.conditions import Key

# --- Config ---
BUCKET = "billing-pipeline-bucket"
REGION = "us-east-1"
PROFILE = "eit"
MODEL_ID = "anthropic.claude-3-5-sonnet-20240620-v1:0"
MAX_TOKENS = 1000
TEMPERATURE = 0.4
TOP_P = 0.7

# --- AWS Session ---
session = boto3.Session(profile_name=PROFILE)
s3 = session.client("s3", region_name=REGION)
textract = session.client("textract", region_name=REGION)
bedrock = session.client("bedrock-runtime", region_name=REGION)
dynamodb = session.resource("dynamodb", region_name=REGION)
table = dynamodb.Table("ClaudeConversations")

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

# --- DynamoDB Helpers ---
def save_to_dynamodb(file_key, entry_type, content):
    table.put_item(Item={
        'file_key': file_key,
        'timestamp': int(time.time()),
        'type': entry_type,
        'content': content
    })

def fetch_recent_history(file_key, limit=5):
    response = table.query(
        KeyConditionExpression=Key('file_key').eq(file_key),
        ScanIndexForward=False,
        Limit=limit
    )
    return [item['content'] for item in response.get('Items', [])]

# --- Claude Prompt Builder ---
def build_prompt(forms, file_key=None):
    base = (
        "You are an AWS cost optimization expert. Based on the bill data below:\n"
        "- Identify the top 3 to 5 highest-cost services.\n"
        "- Recommend precise AWS cost-saving actions.\n"
        "- Skip repeating the bill and avoid generic advice.\n\n"
        "Bill Data:\n" +
        "\n".join(f"{k}: {v}" for k, v in forms)
    )

    messages = [{"role": "user", "content": base}]

    if file_key:
        past = fetch_recent_history(file_key)
        if past:
            messages.insert(0, {
                "role": "user",
                "content": "Here is relevant context from previous analyses:\n\n" + "\n---\n".join(past)
            })
    return messages

# --- Claude Bedrock Call ---
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
st.set_page_config(page_title="Billbot", layout="centered")
st.title("📃 Billbot")

uploaded_file = st.file_uploader("Upload your AWS bill (PDF/JPG/PNG)", type=["pdf", "jpg", "jpeg", "png"])

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
        messages = build_prompt(st.session_state['forms'], st.session_state['file_key'])
        suggestion = ask_claude(messages)
        st.session_state['claude_suggestion'] = suggestion
        st.session_state['qa_pairs'] = []
        save_to_dynamodb(st.session_state['file_key'], "suggestion", suggestion.strip())

if 'claude_suggestion' in st.session_state:
    st.subheader("🤖 Claude’s Cost Optimization Suggestions")
    st.markdown(f"```{st.session_state['claude_suggestion'].strip()}```")

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

        if 'qa_pairs' not in st.session_state:
            st.session_state['qa_pairs'] = []
        st.session_state['qa_pairs'].append((user_query, followup.strip()))
        save_to_dynamodb(st.session_state['file_key'], "qa", f"Q: {user_query}\nA: {followup.strip()}")

    if 'qa_pairs' in st.session_state:
        st.subheader("🗂️ Follow-Up Questions and Answers")
        for i, (q, a) in enumerate(st.session_state['qa_pairs'], 1):
            st.markdown(f"**Q{i}: {q}**")
            st.markdown(f"> {a}")
 

 """

# This code below has a button to print a downloadable pdf report.

import streamlit as st
import boto3
import time
import json
from boto3.dynamodb.conditions import Key
from io import BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

# --- Config ---
BUCKET = "billing-pipeline-bucket"
REGION = "us-east-1"
PROFILE = "eit"
MODEL_ID = "anthropic.claude-3-5-sonnet-20240620-v1:0"
MAX_TOKENS = 1000
TEMPERATURE = 0.4
TOP_P = 0.7

# --- AWS Session ---
session = boto3.Session(profile_name=PROFILE)
s3 = session.client("s3", region_name=REGION)
textract = session.client("textract", region_name=REGION)
bedrock = session.client("bedrock-runtime", region_name=REGION)
dynamodb = session.resource("dynamodb", region_name=REGION)
table = dynamodb.Table("ClaudeConversations")

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

# --- DynamoDB Helpers ---
def save_to_dynamodb(file_key, entry_type, content):
    table.put_item(Item={
        'file_key': file_key,
        'timestamp': int(time.time()),
        'type': entry_type,
        'content': content
    })

def fetch_recent_history(file_key, limit=5):
    response = table.query(
        KeyConditionExpression=Key('file_key').eq(file_key),
        ScanIndexForward=False,
        Limit=limit
    )
    return [item['content'] for item in response.get('Items', [])]

# --- Claude Prompt Builder ---
def build_prompt(forms, file_key=None):
    base = (
        "You are an AWS cost optimization expert. Based on the bill data below, prepare a clean, professional cost optimization report that includes:\n"
        "1. Executive summary of the top 3 to 5 highest-cost AWS services.\n"
        "2. Detailed, actionable recommendations for cost savings tailored to each service.\n"
        "3. Clear formatting with headings, bullet points, and concise language.\n"
        "4. Avoid repeating raw bill data or generic advice.\n\n"
        "Bill Data:\n" +
        "\n".join(f"{k}: {v}" for k, v in forms)
    )

    messages = [{"role": "user", "content": base}]

    if file_key:
        past = fetch_recent_history(file_key)
        if past:
            messages.insert(0, {
                "role": "user",
                "content": "Here is relevant context from previous analyses:\n\n" + "\n---\n".join(past)
            })
    return messages

# --- Claude Bedrock Call ---
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

# --- PDF Report Generator ---
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle

from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle

def generate_pdf_report(report_text):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=50, leftMargin=50, topMargin=50, bottomMargin=50)

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='CustomHeading1', fontSize=14, leading=18, spaceAfter=12, alignment=TA_LEFT, fontName='Helvetica-Bold'))
    styles.add(ParagraphStyle(name='CustomBullet', leftIndent=20, bulletIndent=10, spaceAfter=6, fontSize=10))
    styles.add(ParagraphStyle(name='BodyTextCustom', fontSize=11, leading=15, spaceAfter=10))

    story = []

    # Title
    story.append(Paragraph("AWS Cost Optimization Report", styles['CustomHeading1']))
    story.append(Spacer(1, 12))

    # Process sections
    sections = report_text.strip().split("\n\n")
    for section in sections:
        if section.strip().lower().startswith(("objective", "summary", "analysis", "recommendations")):
            story.append(Paragraph(section.strip(), styles['CustomHeading1']))
        elif section.strip().startswith(("-", "*")):
            for line in section.split('\n'):
                bullet_text = line.strip().lstrip('-*').strip()
                if bullet_text:
                    story.append(Paragraph(f'• {bullet_text}', styles['CustomBullet']))
        else:
            # Regular paragraph
            story.append(Paragraph(section.strip().replace("\n", "<br/>"), styles['BodyTextCustom']))

        story.append(Spacer(1, 6))

    doc.build(story)
    pdf = buffer.getvalue()
    buffer.close()
    return pdf

# --- Streamlit UI ---
st.set_page_config(page_title="Billbot", layout="centered")
st.title("📃 Billbot")

uploaded_file = st.file_uploader("Upload your AWS bill (PDF/JPG/PNG)", type=["pdf", "jpg", "jpeg", "png"])

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
        messages = build_prompt(st.session_state['forms'], st.session_state['file_key'])
        suggestion = ask_claude(messages)
        st.session_state['claude_suggestion'] = suggestion
        st.session_state['qa_pairs'] = []
        save_to_dynamodb(st.session_state['file_key'], "suggestion", suggestion.strip())

if 'claude_suggestion' in st.session_state:
    st.subheader("🤖 Claude’s Cost Optimization Suggestions")
    st.markdown(f"```{st.session_state['claude_suggestion'].strip()}```")

    # PDF download button
    pdf_bytes = generate_pdf_report(st.session_state['claude_suggestion'].strip())
    st.download_button(
        label="📄 Download Professional PDF Report",
        data=pdf_bytes,
        file_name="AWS_Cost_Optimization_Report.pdf",
        mime="application/pdf"
    )

    user_query = st.text_input("💬 Have a follow-up question?")
    if user_query and st.button("Ask Claude"):
        context = (
            "You are an AWS cost optimization expert. Provide a clear, professional answer to the user's follow-up question "
            "based on the AWS bill data and your earlier recommendations. Use concise paragraphs and bullet points where appropriate."
            " Do not repeat earlier suggestions or raw bill data.\n\n"
            "Bill Data:\n" 
        )
        for k, v in st.session_state['forms']:
            context += f"{k}: {v}\n"
        context += f"\nYour Previous Suggestions:\n{st.session_state['claude_suggestion'].strip()}\n\n"
        user_message = f"{context}\nUser's Question: {user_query}"

        with st.spinner("Claude is answering..."):
            followup = ask_claude([{ "role": "user", "content": user_message }])

        if 'qa_pairs' not in st.session_state:
            st.session_state['qa_pairs'] = []
        st.session_state['qa_pairs'].append((user_query, followup.strip()))
        save_to_dynamodb(st.session_state['file_key'], "qa", f"Q: {user_query}\nA: {followup.strip()}")

if 'qa_pairs' in st.session_state:
    st.subheader("🤔 Follow-Up Questions and Answers")
    for i, (q, a) in enumerate(st.session_state['qa_pairs'], 1):
        st.markdown(f"**Q{i}: {q}**")
        st.markdown(f"> {a}")