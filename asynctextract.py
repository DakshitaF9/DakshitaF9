import boto3
import time

# ----------- CONFIGURATION -----------
region = 'us-east-1'
bucket_name = 'billing-pipeline-bucket'    
file_name = 'lakshya bill.pdf'          
s3_object = {'Bucket': bucket_name, 'Name': file_name}
feature_types = ['TABLES', 'FORMS']
# -------------------------------------

# Connect to Textract
textract = boto3.client('textract', region_name=region)

# ----------- START ANALYSIS JOB -----------
response = textract.start_document_analysis(
    DocumentLocation={'S3Object': s3_object},
    FeatureTypes=feature_types
)

job_id = response['JobId']
print(f" Textract Job Started: {job_id}")

# ----------- WAIT FOR COMPLETION -----------
print("Waiting for job to complete...")
while True:
    result = textract.get_document_analysis(JobId=job_id)
    status = result['JobStatus']
    if status in ['SUCCEEDED', 'FAILED']:
        print(f" Job finished with status: {status}")
        break
    time.sleep(5)

if status == 'FAILED':
    raise Exception("Textract job failed")

# ----------- GET ALL PAGES OF BLOCKS -----------
pages = []
next_token = None

while True:
    if next_token:
        response = textract.get_document_analysis(JobId=job_id, NextToken=next_token)
    else:
        response = textract.get_document_analysis(JobId=job_id)

    pages.extend(response['Blocks'])
    next_token = response.get('NextToken')
    if not next_token:
        break

print(f"Total blocks returned: {len(pages)}")

# Use `pages` as Blocks input for Forms + Tables
block_map = {block['Id']: block for block in pages}

# ----------- EXTRACT FORMS (KEY-VALUE PAIRS) -----------
print("\n=== FORMS: KEY-VALUE PAIRS ===")

for block in pages:
    if block['BlockType'] == 'KEY_VALUE_SET' and 'KEY' in block.get('EntityTypes', []):
        key_text = ''
        value_text = ''

        # Get the key text
        for rel in block.get('Relationships', []):
            if rel['Type'] == 'CHILD':
                for child_id in rel['Ids']:
                    word = block_map.get(child_id)
                    if word and word['BlockType'] == 'WORD':
                        key_text += word['Text'] + ' '

        # Get the value text
        for rel in block.get('Relationships', []):
            if rel['Type'] == 'VALUE':
                for value_id in rel['Ids']:
                    value_block = block_map.get(value_id)
                    for vrel in value_block.get('Relationships', []):
                        if vrel['Type'] == 'CHILD':
                            for child_id in vrel['Ids']:
                                word = block_map.get(child_id)
                                if word and word['BlockType'] == 'WORD':
                                    value_text += word['Text'] + ' '

        print(f"{key_text.strip()}: {value_text.strip()}")

# ----------- EXTRACT TABLES -----------
print("\n=== TABLES (CSV Format) ===")
tables = []

for block in pages:
    if block['BlockType'] == 'CELL':
        row_index = block['RowIndex']
        col_index = block['ColumnIndex']
        text = ''

        if 'Relationships' in block:
            for rel in block['Relationships']:
                if rel['Type'] == 'CHILD':
                    for id in rel['Ids']:
                        word = block_map.get(id)
                        if word and word['BlockType'] == 'WORD':
                            text += word['Text'] + ' '

        tables.append((row_index, col_index, text.strip()))

# Organize into 2D list (table structure)
if tables:
    max_row = max(r for r, _, _ in tables)
    max_col = max(c for _, c, _ in tables)
    table_data = [['' for _ in range(max_col)] for _ in range(max_row)]

    for row, col, val in tables:
        table_data[row - 1][col - 1] = val

    for row in table_data:
        print(','.join(cell if cell else '' for cell in row))
else:
    print("No tables found.")



# ----------- SAVE TO FILE FOR CLAUDE INPUT -----------
with open("parsed_bill_text.txt", "w") as f:
    f.write("=== FORMS: KEY-VALUE PAIRS ===\n")
    
    for block in pages:
        if block['BlockType'] == 'KEY_VALUE_SET' and 'KEY' in block.get('EntityTypes', []):
            key_text = ''
            value_text = ''
            for rel in block.get('Relationships', []):
                if rel['Type'] == 'CHILD':
                    for child_id in rel['Ids']:
                        word = block_map.get(child_id)
                        if word and word['BlockType'] == 'WORD':
                            key_text += word['Text'] + ' '
                if rel['Type'] == 'VALUE':
                    for value_id in rel['Ids']:
                        value_block = block_map.get(value_id)
                        for vrel in value_block.get('Relationships', []):
                            if vrel['Type'] == 'CHILD':
                                for child_id in vrel['Ids']:
                                    word = block_map.get(child_id)
                                    if word and word['BlockType'] == 'WORD':
                                        value_text += word['Text'] + ' '
            f.write(f"{key_text.strip()}: {value_text.strip()}\n")

    f.write("\n=== TABLES (CSV Format) ===\n")
    if tables:
        for row in table_data:
            f.write(','.join(cell if cell else '' for cell in row) + '\n')
    else:
        f.write("No tables found.\n")
