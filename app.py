# This code has a permanent graphical representation.

import streamlit as st

st.set_page_config(page_title="Billbot", layout="centered")

import boto3
import time
import re
import json
import numpy as np
import matplotlib.pyplot as plt
from boto3.dynamodb.conditions import Key
from io import BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from dotenv import load_dotenv
import os

# Load environment variables from the .env file
load_dotenv()

# Access variables like this
BUCKET = os.getenv("AWS_BUCKET")
REGION = os.getenv("AWS_REGION")
PROFILE = os.getenv("AWS_PROFILE")
MODEL_ID = os.getenv("CLAUDE_MODEL_ID")
MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS"))
TEMPERATURE = float(os.getenv("CLAUDE_TEMPERATURE"))
TOP_P = float(os.getenv("CLAUDE_TOP_P"))
table = os.getenv("DYNAMO_TABLE")

# --- AWS Session ---
#session = boto3.Session(profile_name=PROFILE)
session = boto3.Session()
s3 = session.client("s3", region_name=REGION)
textract = session.client("textract", region_name=REGION)
bedrock = session.client("bedrock-runtime", region_name=REGION)
dynamodb = session.resource("dynamodb", region_name=REGION)
table = dynamodb.Table(table)

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
        kwargs = {'JobId': job_id}
        if next_token:
            kwargs['NextToken'] = next_token
        result = textract.get_document_analysis(**kwargs)
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

    base += ("\n Also, return a JSON array (at the very end) showing correctly estimated cost before and after optimization for each major AWS service in the following format :\n"
            '[{"service": "EC2", "before": 300, "after": 200}, {"service": "S3", "before": 120, "after": 100}]\n'
    "Only return this JSON array after all recommendations, so it can be used for plotting.")

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

def extract_cost_json_from_suggestion(text):
    try: 
        json_match = re.search(r'\[\s*\{.*?\}\s*\]', text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        st.warning(f"Could not parse cost chart data: {e}")
    return []

def extract_suggestions_only(text):
    # Remove the JSON array at the end of the text (if present)
    return re.sub(r'\[\s*\{.*\}\s*\]\s*$', '', text.strip(), flags=re.DOTALL).strip()

# --- PDF Report Generator ---

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
#st.set_page_config(page_title="Billbot", layout="centered")
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

if 'forms' in st.session_state and st.button("3️⃣ Get Claude Optimization Suggestions"):
    with st.spinner("Claude is thinking..."):
        messages = build_prompt(st.session_state['forms'], st.session_state['file_key'])
        suggestion = ask_claude(messages)
        st.session_state['claude_suggestion'] = suggestion
        st.session_state['cost_chart_data'] = extract_cost_json_from_suggestion(suggestion)
        st.session_state['qa_pairs'] = []
        save_to_dynamodb(st.session_state['file_key'], "suggestion", suggestion.strip())

if "claude_suggestion" in st.session_state:
    st.subheader("🤖 Claude’s Cost Optimization Suggestions")
    suggestions_only = extract_suggestions_only(st.session_state['claude_suggestion'])
    st.markdown(f"```{suggestions_only}```")

    # Adding a flag
if "cost_plot_shown" not in st.session_state:
    st.session_state["cost_plot_shown"] = False

# When user presses the button once, store flag
if st.button("📊 Show Cost Optimization Visualization"):
    st.session_state["cost_plot_shown"] = True

# If flag is True, always show the plot
if st.session_state["cost_plot_shown"]:
    suggestion_text = st.session_state["claude_suggestion"]
    json_block = None

    try:
        json_candidates = re.findall(r'\[\s*{.*?}\s*\]', suggestion_text, re.DOTALL)
        if json_candidates:
            json_block = json.loads(json_candidates[-1])
    except Exception as e:
        st.warning("⚠️ Failed to parse JSON from Claude's response.")
        st.stop()

    if not json_block:
        st.error("❌ No cost optimization data found in Claude’s response.")
        st.stop()

    # Filter and format cost optimization data
    filtered_data = [
        item for item in json_block
        if abs(item["before"] - item["after"]) >= 2
    ]

    if not filtered_data:
        st.info("ℹ️ No significant optimizations (≥ $2 savings) to plot.")
        st.stop()

    def format_service_name(name):
        return name.upper().replace("_", " ").replace("-", " ").title()

    services = [format_service_name(item["service"]) for item in filtered_data]
    before_costs = np.array([item["before"] for item in filtered_data])
    after_costs = np.array([item["after"] for item in filtered_data])

    # Plotting
    x = np.arange(len(services))
    width = 0.35
    fig, ax = plt.subplots(figsize=(10, 6))
    bars1 = ax.bar(x - width/2, before_costs, width, label='Before Optimization', color='tomato', edgecolor='black')
    bars2 = ax.bar(x + width/2, after_costs, width, label='After Optimization', color='seagreen', edgecolor='black')

    ax.set_ylabel('Monthly Cost (USD)', fontsize=12)
    ax.set_title('AWS Services with Significant Cost Optimization', fontsize=14, weight='bold')
    ax.set_xticks(x)
    ax.set_xticklabels(services, ha='right', fontsize=10, rotation=30)
    ax.legend(fontsize=10)
    ax.grid(axis='y', linestyle='--', alpha=0.7)
    ax.bar_label(bars1, padding=3, fmt='%.2f', fontsize=8)
    ax.bar_label(bars2, padding=3, fmt='%.2f', fontsize=8)

    plt.tight_layout()
    st.pyplot(fig)

    # PDF download button
    clean_suggestion = extract_suggestions_only(st.session_state['claude_suggestion'])
    pdf_bytes = generate_pdf_report(clean_suggestion)
    st.download_button(
        label="📄 Download PDF Report",
        data=pdf_bytes,
        file_name="Cost_Report.pdf",
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
        for i, (q, a) in enumerate(st.session_state['qa_pairs'], 1):
            st.markdown(f"**Q{i}: {q}**")
            st.markdown(f"> {a}")