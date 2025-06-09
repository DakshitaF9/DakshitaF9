import boto3
import json

# ----------- CONFIGURATION -----------
region = 'us-east-1'
model_id = 'anthropic.claude-instant-v1'
input_file = 'parsed_bill_text.txt'
max_tokens = 1000
temperature = 0.7
top_p = 0.9
top_k = 250
# -------------------------------------

# Read Textract results from file
with open(input_file, 'r') as f:
    document_text = f.read()

# Create Claude prompt
prompt = f"""Human: You are an AWS cost optimization expert. Based on the bill data below:

- Identify the top 3–5 highest-cost services.
- Recommend precise AWS cost-saving actions (e.g., gp3, CloudFront, savings plans, VPC endpoints).
- Skip repeating the bill and avoid generic advice.

Bill Data:
{document_text}

Assistant:"""

# Create Bedrock client
bedrock = boto3.client('bedrock-runtime', region_name=region)

# Properly build the JSON body
body = {
    "prompt": prompt,
    "max_tokens_to_sample": max_tokens,
    "temperature": temperature,
    "top_k": top_k,
    "top_p": top_p,
    "stop_sequences": ["\n\nHuman:"]
}

# Invoke Claude
response = bedrock.invoke_model(
    modelId=model_id,
    contentType='application/json',
    accept='application/json',
    body=json.dumps(body).encode('utf-8')  # 💡 Correct format
)

# Parse and print result
result_json = json.loads(response['body'].read().decode('utf-8'))

print("\n🧠 Claude's Suggestions:\n")
print(result_json.get("completion", "❌ No response from Claude"))

