import uuid
from datetime import datetime
from azure.cosmos import CosmosClient, exceptions
import json
import logging
import os
import requests
import azure.functions as func
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError
import fitz  # PyMuPDF
from urllib.parse import urljoin
from azure.identity import DefaultAzureCredential
from docx2pdf import convert

# Initialize Cosmos DB client
COSMOS_URI = os.environ["COSMOS_DB_URI"]
COSMOS_KEY = os.environ["COSMOS_DB_KEY"]
COSMOS_DATABASE = os.environ["COSMOS_DB_DATABASE"]
COSMOS_CONTAINER = os.environ["COSMOS_DB_CONTAINER"]

#cosmos_client = CosmosClient(COSMOS_URI, DefaultAzureCredential())
#database = cosmos_client.get_database_client(COSMOS_DATABASE)
#container = database.get_container_client(COSMOS_CONTAINER)

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

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

@app.route(route="document_processing")
def document_processing(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function processed a request.')

    # Get the document name from query parameters
    document_name = req.params.get('document_name')
    if not document_name:
        store_response_in_cosmos(
            status="failed: missing document_name",
            http_status_code=400,
            document_name=document_name or "unknown",
            text_content="",
            response_json={"error": "Missing 'document_name' parameter."}
        )
        return func.HttpResponse(
            "Please provide a 'document_name' parameter in the query string.",
            status_code=400
        )

    try:
        # Initialize Blob Storage client
        blob_service_client = BlobServiceClient.from_connection_string(
            os.environ['BLOB_CONNECTION_STRING']
        )
        container_name = 'documents'
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=document_name)

        # Download the document
        downloader = blob_client.download_blob()
        document_content = downloader.readall()
        logging.info(f"Downloaded document '{document_name}' from container '{container_name}'.")
    except ResourceNotFoundError:
        logging.error(f"Document '{document_name}' not found in container '{container_name}'.")
        store_response_in_cosmos(
            status="failed: document not found",
            http_status_code=404,
            document_name=document_name,
            text_content="",
            response_json={"error": f"Document '{document_name}' not found."}
        )
        return func.HttpResponse(
            f"Document '{document_name}' not found.",
            status_code=404
        )
    except Exception as e:
        logging.error(f"Error downloading document: {e}")
        store_response_in_cosmos(
            status=f"failed: error downloading document - {e}",
            http_status_code=500,
            document_name=document_name,
            text_content="",
            response_json={"error": "Error downloading the document."}
        )
        return func.HttpResponse(
            "Error downloading the document.",
            status_code=500
        )

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
                        status=f"failed: error uploading image '{image_filename}' - {e}",
                        http_status_code=500,
                        document_name=document_name,
                        text_content=txt_content,
                        response_json={"error": f"Error uploading image '{image_filename}': {e}"}
                    )
                    return func.HttpResponse(
                        "Error uploading images to Blob Storage.",
                        status_code=500
                    )

        pdf_doc.close()
    except Exception as e:
        logging.error(f"Error analyzing document: {e}")
        store_response_in_cosmos(
            status=f"failed: error analyzing document - {e}",
            http_status_code=500,
            document_name=document_name,
            text_content="",
            response_json={"error": "Error loading the document."}
        )
        return func.HttpResponse(
            "Error loading the document.",
            status_code=500
        )

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

    ENDPOINT = os.environ["AZURE_OPENAI_API_ENDPOINT"]
    API_KEY = os.environ["OPENAI_API_KEY"]

    headers = {
        "Content-Type": "application/json",
        "api-key": API_KEY,
    }

    # Send request to Azure OpenAI
    try:
        response = requests.post(ENDPOINT, headers=headers, json=payload)
        response.raise_for_status()  # Will raise an HTTPError if the HTTP request returned an unsuccessful status code
        json_response = response.json()
    except requests.RequestException as e:
        logging.error(f"Failed to make the Azure Open AI request. Error: {e}")
        store_response_in_cosmos(
            status=f"failed: OpenAI request error - {e}",
            http_status_code=500,
            document_name=document_name,
            text_content=txt_content,
            response_json={"error": f"Failed to make the Azure Open AI request. Error: {e}"}
        )
        return func.HttpResponse(
            "Failed to make the Azure Open AI request.",
            status_code=500
        )

    if 'choices' in json_response and len(json_response['choices']) > 0:
        # Extract the content field from the first choice
        message = json_response['choices'][0].get('message', {})
        content = message.get('content', None)
        if content:
            cleaned_content = content.strip("```json\n").strip("```")
            try:
                response_data = json.loads(cleaned_content)
                # Store success in Cosmos DB
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
                # Store failure status
                store_response_in_cosmos(
                    status="failed: JSON decode error",
                    http_status_code=500,
                    document_name=document_name,
                    text_content=txt_content,
                    response_json={"error": "Invalid JSON format in response."}
                )
                return func.HttpResponse(
                    "Error processing the document with OpenAI.",
                    status_code=500
                )
        else:
            logging.error("No content found in the OpenAI response.")
            # Store failure status
            store_response_in_cosmos(
                status="failed: no content in response",
                http_status_code=500,
                document_name=document_name,
                text_content=txt_content,
                response_json=json_response
            )
            return func.HttpResponse(
                "No content found in the OpenAI response.",
                status_code=500
            )
    else:
        logging.error("No choices or invalid response format.")
        # Store failure status
        store_response_in_cosmos(
            status="failed: no choices or invalid format",
            http_status_code=500,
            document_name=document_name,
            text_content=txt_content,
            response_json=json_response
        )
        return func.HttpResponse(
            "No choices or invalid response format.",
            status_code=500
        )