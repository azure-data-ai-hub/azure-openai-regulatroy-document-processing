import uuid
from datetime import datetime
from azure.cosmos import CosmosClient, exceptions
import json
import logging
import os
import requests
import azure.functions as func
from azure.storage.blob import BlobServiceClient
import fitz  # PyMuPDF
from urllib.parse import urljoin
import openai

# Initialize Cosmos DB client
COSMOS_URI = os.environ["COSMOS_DB_URI"]
COSMOS_KEY = os.environ["COSMOS_DB_KEY"]
COSMOS_DATABASE = "bpadocumentdb"
COSMOS_CONTAINER = "bpadocumentcontainer"

#cosmos_client = CosmosClient(COSMOS_URI, DefaultAzureCredential())
#database = cosmos_client.get_database_client(COSMOS_DATABASE)
#container = database.get_container_client(COSMOS_CONTAINER)

blob_service_client = BlobServiceClient.from_connection_string(
            os.environ['BLOB_CONNECTION_STRING']
        )

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

@app.route(route="document_processing")
def document_processing(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function processed a request.')

    document_name = req.params.get('document_name')
    if not document_name:
        store_response_in_cosmos(
            status="failed",
            http_status_code=400,
            document_name="unknown",
            text_content="",
            response_json={"error": "Missing 'document_name' parameter."}
        )
        return func.HttpResponse(
            "Please provide a 'document_name' parameter in the query string.",
            status_code=400
        )

    try:
        document_content = download_document(document_name)
        txt_content = process_document(document_name, document_content)
        payload = generate_prompt(txt_content, document_name)
        json_response = call_openai_api(payload)
        cleaned_content = json_response.choices[0].message.content.strip("```json\n").strip("```") 

        if cleaned_content:
            try:
                response_data = json.loads(cleaned_content)
                store_response_in_cosmos(
                    status="success",
                    http_status_code=200,
                    document_name=document_name,
                    text_content=txt_content,
                    response_json=response_data
                )
                return func.HttpResponse(
                    json.dumps(response_data),
                    mimetype="application/json",
                    status_code=200
                )
            except json.JSONDecodeError as e:
                logging.error(f"JSON decode error: {e}")
                store_response_in_cosmos(
                    status="failed",
                    http_status_code=500,
                    document_name=document_name,
                    text_content=txt_content,
                    response_json={"error": str(e)}
                )
                return func.HttpResponse(
                    "Failed to parse the response from OpenAI.",
                    status_code=500
                )

        logging.error("No content in the OpenAI response.")
        
        return func.HttpResponse(
            "No content in the OpenAI response.",
            status_code=500
        )

    except Exception as e:
        logging.error(f"An internal server error occurred: {e}")
        store_response_in_cosmos(
            status="failed",
            http_status_code=500,
            document_name=document_name,
            text_content=txt_content,
            response_json={"error": str(e)}
        )

        return func.HttpResponse(
            "An internal server error occurred.",
            status_code=500
        )
        

def download_document(document_name):
    try:
        container_name = 'documents'
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=document_name)

        stream_downloader = blob_client.download_blob()
        document_content = stream_downloader.readall()
        logging.info(f"Document {document_name} downloaded successfully.")
        return document_content
    except Exception as e:
        logging.error(f"Failed to download document {document_name}: {e}")
        raise e

def process_document(document_name, document_content):
    try:
        # Open the PDF document
        pdf_doc = fitz.open(stream=document_content, filetype="pdf")
        txt_content = ""
        for page_num in range(len(pdf_doc)):
            page = pdf_doc.load_page(page_num)
            txt_content += page.get_text()

            # Handle images within the PDF
            image_list = page.get_images(full=True)
            for img_index, img in enumerate(image_list, start=1):
                xref = img[0]
                base_image = pdf_doc.extract_image(xref)
                image_bytes = base_image["image"]
                image_ext = base_image["ext"]
                unique_id = uuid.uuid4().hex
                image_filename = f"image_{unique_id}.{image_ext}"

                try:
                    blob_client_image = blob_service_client.get_blob_client(container="images", blob=image_filename)
                    
                    # Upload the image
                    blob_client_image.upload_blob(image_bytes, overwrite=True)
                    logging.info(f"Uploaded image '{image_filename}' to container 'images'.")
                    
                    # Construct the image URL
                    image_url = urljoin(os.environ["BLOB_BASE_URL"], image_filename)
                    # Insert image URL as placeholder
                    txt_content += f"![Image]({image_url})\n"
                except Exception as e:
                    logging.error(f"Error uploading image '{image_filename}': {e}")
                    store_response_in_cosmos(
                        status=f"failed",
                        http_status_code=500,
                        document_name=document_name,
                        text_content=txt_content,
                        response_json={"error": f"Error uploading image '{image_filename}': {e}"}
                    )
                    raise e

        pdf_doc.close()
    except Exception as e:
        logging.error(f"Failed to process the document {document_name}: {e}")
        raise e
    
    return txt_content

def generate_prompt(txt_content, document_name):
    # Define your prompt with the extracted text
    prompt = f"""
    You are an AI assistant tasked with extracting all the questions and content from the given regulatory document. The questions and content may be embedded within paragraphs, listed directly, or mentioned in various sections. Ensure that all questions and content are identified and listed clearly in a hierarchical order. Additionally, provide the extracted questions or content in a table format with the columns: Section Name, Number, Text, and Context. Embed image descriptions and URLs directly within the "Text" field using Markdown image syntax where applicable.

    Document Content:
    \"\"\"
    {txt_content}
    \"\"\"

    Extraction Requirements:
    1. **Sections**
        - Identify all sections and their respective headings.
        - Provide the content under each section.

    2. **Questions and Content**
        - Extract all questions and relevant content, whether they are embedded within paragraphs, listed directly, or mentioned in various sections.
        - Ensure that the hierarchical structure (e.g., sections, subsections, items) is maintained.
        - Include lists, tables, and images within the content text as applicable.

    3. **Lists**
        - Identify and extract all lists, including nested lists.
        - Preserve the hierarchy and relationship between list items.
        - Embed lists within the "Text" field using a structured format (e.g., Markdown or JSON arrays).

    4. **Tables**
        - Identify all tables within the document.
        - Extract table titles and data in a structured format.
        - Embed tables within the "Text" field using a structured format (e.g., Markdown tables or JSON arrays).

    5. **Images**
        - Describe each image found in the document.
        - Include the corresponding image URLs.
        - Embed image descriptions and URLs within the "Text" field using Markdown image syntax.

    Output Format:
    Return the extracted information in JSON format with the following structure:
    {{
        "extracted_data": [
            {{
                "section": "Section Name",
                "number": "Question or Content Number",
                "text": "Question or Content Text with embedded lists, tables, and ![Image Description](Image_URL).",
                "context": "Context or Reference"
            }},
            ...
        ]
    }}
    """

    payload = {
        "messages": [
            {
                "role": "system",
                "content": "You are an AI assistant that collates questions/content from documents submitted by regulatory intervenors."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.7,
        "top_p": 0.95
    }
    
    return payload

    # Send request to Azure OpenAI

def call_openai_api(payload):
    ENDPOINT = os.environ["AZURE_OPENAI_API_ENDPOINT"]
    API_KEY = os.environ["OPENAI_API_KEY"]

    client = openai.AzureOpenAI(
        api_key=API_KEY,
        api_version="2024-02-15-preview",
        azure_endpoint = ENDPOINT
    )
    # Create and return a new chat completion request
    return client.chat.completions.create(
        model="gpt-4o-mini",
        messages=payload['messages'],
        stream=False
    )

def call_openai_url(payload):
    ENDPOINT = os.environ["AZURE_OPENAI_API_ENDPOINT"]
    API_KEY = os.environ["OPENAI_API_KEY"]

    headers = {
        "Content-Type": "application/json",
        "api-key": API_KEY,
    }

    try:
        response = requests.post(ENDPOINT, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logging.error(f"Failed to make the Azure OpenAI request. Error: {e}")
        raise e
    
def store_response_in_cosmos(status, http_status_code, document_name, text_content, response_json):
    try:
        item = {
            "id": f"{document_name}-{uuid.uuid4()}",
            "status": status,
            "http_status_code": http_status_code,
            "document_name": document_name,
            "text_content": text_content,
            "response_json": response_json,
            "timestamp": datetime.utcnow().isoformat()
        }
        #container.upsert_item(item)
        logging.info("Response stored in Cosmos DB successfully.")
    except exceptions.CosmosHttpResponseError as e:
        logging.error(f"Failed to store response in Cosmos DB: {e}")
