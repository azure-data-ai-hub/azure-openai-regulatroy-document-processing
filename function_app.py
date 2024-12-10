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
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeResult, AnalyzeOutputOption, AnalyzeDocumentRequest
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError



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
        txt_content = process_document_DI(document_name, document_content)
        payload = generate_prompt_url(txt_content)
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

def process_document_DI(document_name, document_content):
    try:
        FORM_RECOGNIZER_ENDPOINT = os.environ["FORM_RECOGNIZER_ENDPOINT"]
        FORM_RECOGNIZER_KEY = os.environ["FORM_RECOGNIZER_KEY"]

        document_intelligence_client = DocumentIntelligenceClient(
        endpoint=FORM_RECOGNIZER_ENDPOINT, credential=AzureKeyCredential(FORM_RECOGNIZER_KEY)
        )

        poller = document_intelligence_client.begin_analyze_document(
            "prebuilt-layout", analyze_request=AnalyzeDocumentRequest(bytes_source=document_content), output=[AnalyzeOutputOption.FIGURES]
        )

        result: AnalyzeResult = poller.result()
        operation_id = poller.details["operation_id"]
        
        # Dictionary to store figures per page
        page_figures = {}

        if result.figures:
            for figure in result.figures:
                if figure.id:
                    # Retrieve the figure image
                    response = document_intelligence_client.get_analyze_result_figure(
                        model_id=result.model_id,
                        result_id=operation_id,
                        figure_id=figure.id
                    )
                    # Read the content from response
                    image_bytes = b''.join(response)

                    # Upload the image to Blob Storage
                    blob_name = f"{document_name}_{figure.id}.png"
                    blob_client = blob_service_client.get_blob_client(container="images", blob=blob_name)
                    blob_client.upload_blob(image_bytes, overwrite=True)

                    # Get the image URL
                    image_url = blob_client.url

                    # Get the page number
                    if figure.bounding_regions:
                        page_number = figure.bounding_regions[0].page_number
                    else:
                        page_number = 1  # Default or handle appropriately

                    # Collect the figure data per page
                    if page_number not in page_figures:
                        page_figures[page_number] = []
                    page_figures[page_number].append({
                        'caption': figure.caption.content if figure.caption else '',
                        'image_url': image_url
                    })

        if not result.pages:
            print("No pages detected in the document.")
            extracted_content = ""
        else:
            # Extract content from the result
            extracted_content = ""

            for page in result.pages:
                page_number = page.page_number

                # Get figures for the current page
                figures_on_page = page_figures.get(page_number, [])

                # Build a list of captions to image URLs for matching
                caption_to_image = {fig['caption']: fig['image_url'] for fig in figures_on_page}

                # Process the page lines
                if hasattr(page, 'lines') and page.lines:
                    for line in page.lines:
                        line_text = line.content
                        extracted_content += line_text + "\n"
                        
                        # Check if the line matches any figure caption
                        if line_text in caption_to_image:
                            # Insert the image URL after the caption
                            image_url = caption_to_image[line_text]
                            extracted_content += f"Image URL: {image_url}\n"
                elif hasattr(page, 'words') and page.words:
                    # Fallback if lines are not available
                    page_text = ' '.join(word.content for word in page.words)
                    extracted_content += page_text + "\n"
                else:
                    print(f"No text content found on page {page_number}.")
            
            print(f"Extracted text from document '{document_name}' with images inserted.")
    except HttpResponseError as e:
        print(f"HTTP error during document analysis: {e.message}")
        raise e
    except Exception as e:
        print(f"Unexpected error analyzing document: {e}")
        raise e

    return extracted_content

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

def generate_prompt(txt_content):
    # Define your prompt with the extracted text
    payload = {
            "messages": [
                {
                "role": "system",
                "content": [
                    {
                    "type": "text",
                    "text": "You are an AI assistant tasked with extracting all the questions and content from the interveners who submitted questions on a regulatory document. The questions and content may be embedded within paragraphs, listed directly, or mentioned in various sections. Ensure that all questions and content are identified and listed clearly in a hierarchical order. Additionally, provide the extracted questions or content in JSON.\n"
                    }
                ]
                },
                {
                "role": "user",
                "content": [
                    {
                    "type": "text",
                    "text": "XXXX, a leading North American energy infrastructure company, has a rich history of innovation and growth. Formed in 1998 through the merger of Pacific Enterprises and Enova Corporation, XXXXX has consistently pursued a path of technological advancement and community-focused initiatives.\n\nXXXXX’s Journey of Innovation and Growth\n\nTo:\n\nJohn Ivy, on behalf of XXXXX, Jivy@sdge.com\n\nJohn Appeased, on behalf of SCG, JAPP.com\n\nDate Sent: September 19, 2035\n\nResponse Due: October 3, 2045\n\nPlease provide a response to the following: XXXXX, a leading North American energy infrastructure company, has a rich history of innovation and growth. Formed in 1998 through the merger of Pacific Enterprises and Enova Corporation, XXXXX has consistently pursued a path of technological advancement and community-focused initiatives.\n\nFrom its inception, XXXXX aimed to deliver energy differently. The company quickly became a significant player in the energy sector, serving millions of consumers across Southern California. Over the years, XXXXX expanded its reach and capabilities, acquiring key assets and investing in renewable energy projects.\n\nPlease note that the questions in this data request relate to both SoCalGas and SDG&E.\n\nGENERAL INSTRUCTIONS\n\n1. One of XXXXX’s notable achievements was the launch of the Energía Costa Azul LNG terminal in Baja California in 2008, one of the first liquefied natural gas (LNG) receipt terminals on the West Coast of North America. This project marked a significant milestone in XXXXX’s commitment to providing reliable and clean energy solutions.\n\n2. Two of XXXXX’s notable achievements was the launch of the Energía Costa Azul LNG terminal in Baja California in 2008, one of the first liquefied natural gas (LNG) receipt terminals on the West Coast of North America. This project marked a significant milestone in XXXXX’s commitment to providing reliable and clean energy solutions.\n\n3. Three of One of XXXXX’s notable achievements was the launch of the Energía Costa Azul LNG terminal in Baja California in 2008, one of the first liquefied natural gas (LNG) receipt terminals on the West Coast of North America. This project marked a significant milestone in XXXXX’s commitment to providing reliable and clean energy solutions.\n\n4. Four of One of XXXXX’s notable achievements was the launch of the Energía Costa Azul LNG terminal in Baja California in 2008, one of the first liquefied natural gas (LNG) receipt terminals on the West Coast of North America. This project marked a significant milestone in XXXXX’s commitment to providing reliable and clean energy solutions.\n\n5. Responses to these data requests should be transmitted as they become available.\n\nDEFINITIONS A. ASSETS: Oil and gas companies often have extensive assets, including oil fields, refineries, pipelines, and storage facilities. B. AMERICAN GIANTS: Major U.S.-based oil companies include ExxonMobil and Chevron, both of which are leaders in exploration, production, and refining. C. ACQUISITIONS: The industry frequently sees mergers and acquisitions, as companies aim to expand their reserves and market share. D. ADVANCEMENTS: Technological advancements, such as hydraulic fracturing and deep-water drilling, have significantly increased oil and gas production capabilities. E. ALTERNATIVE ENERGY: Many oil and gas companies are investing in alternative energy sources, including wind, solar, and hydrogen, to diversify their energy portfolios. F. ASIA: Companies like PetroChina and Sinopec are major players in the Asian market, contributing significantly to global oil and gas production. G. ANALYSIS: Detailed geological and seismic analysis is crucial for identifying potential oil and gas reserves. H. AFFILIATES: Large oil companies often have numerous affiliates and subsidiaries involved in various aspects of the energy sector.\n\nI. AGRE J. EMENTS: International agreements and partnerships are common, allowing companies to explore and produce oil and gas in different regions. K. AUTOMATION: The use of automation and digital technologies is increasing in the oil and gas industry to improve efficiency and safety. L. ALLOCATIONS: Capital allocation strategies are critical for oil and gas companies to balance investments in traditional and renewable energy projects. TEST SET OF DATA REQUESTS For Fuel\n\nGiven XXXXX's historical emphasis on both traditional and renewable energy sources, it is essential to scrutinize the financial and operational aspects related to fuel procurement and distribution. Understanding these metrics will help evaluate the cost-effectiveness and sustainability of XXXXX's fuel-related operations.\n\nFuel Procurement and Cost Analysis a) Please provide a detailed breakdown of the cost structure associated with fuel procurement over the past decade, specifically distinguishing between traditional fossil fuels and renewable energy sources. (Reference: \"XXXXX expanded its reach and capabilities, acquiring key assets and investing in renewable energy projects.\") i. What percentage of the total fuel procurement budget was allocated to renewable energy sources each year? ii. How has the cost per unit of fuel evolved over the years for both traditional and renewable sources? iii. What suppliers and partners have been involved in the procurement process for both types of fuel?\nOperational Efficiency and Environmental Impact a) Describe the measures taken by XXXXX to improve operational efficiency in fuel usage. (Reference: \"XXXXX has consistently pursued a path of technological advancement and community-focused initiatives.\") i. How have these measures impacted the overall fuel consumption and associated costs? ii. Provide data on the reduction in greenhouse gas emissions resulting from these efficiency improvements. iii. What technologies or innovations have been implemented to achieve these efficiencies?\nFor Gas\n\nXXXXX's operations in natural gas, particularly with its notable Energía Costa Azul LNG terminal, necessitate a comprehensive understanding of the financial, operational, and environmental aspects related to gas infrastructure and distribution.\n\nLNG Terminal Operations a) Provide a detailed operational report on the Energía Costa Azul LNG terminal since its inception in 2008. (Reference: \"One of XXXXX’s notable achievements was the launch of the Energía Costa Azul LNG terminal in Baja California in 2008.\") i. What are the annual throughput volumes of LNG at the terminal? ii. What are the main sources of LNG supplied to the terminal, and what percentage comes from renewable sources? iii. How has the terminal's operational efficiency evolved over the years?"
                    }
                ]
                },
                {
                "role": "assistant",
                "content": [
                    {
                    "type": "text",
                    "text": " { \"extracted_data\": [\n{\n\"section\": \"Fuel Procurement and Cost Analysis\",\n\"number\": \"1.a.i\",\n\"question\": \"What percentage of the total fuel procurement budget was allocated to renewable energy sources each year?\",\n\"context\": \"Please provide a detailed breakdown of the cost structure associated with fuel procurement over the past decade, specifically distinguishing between traditional fossil fuels and renewable energy sources. (Reference: 'XXXXX expanded its reach and capabilities, acquiring key assets and investing in renewable energy projects.')\"\n},\n{\n\"section\": \"Fuel Procurement and Cost Analysis\",\n\"number\": \"1.a.ii\",\n\"question\": \"How has the cost per unit of fuel evolved over the years for both traditional and renewable sources?\",\n\"context\": \"Please provide a detailed breakdown of the cost structure associated with fuel procurement over the past decade, specifically distinguishing between traditional fossil fuels and renewable energy sources. (Reference: 'XXXXX expanded its reach and capabilities, acquiring key assets and investing in renewable energy projects.')\"\n}, {\n\"section\": \"Fuel Procurement and Cost Analysis\",\n\"number\": \"1.a.ii\",\n\"question\": \"What suppliers and partners have been involved in the procurement process for both types of fuel?\",\n\"context\": \"Please provide a detailed breakdown of the cost structure associated with fuel procurement over the past decade, specifically distinguishing between traditional fossil fuels and renewable energy sources. (Reference: 'XXXXX expanded its reach and capabilities, acquiring key assets and investing in renewable energy projects.')\"\n},\n{\n\"section\": \"Operational Efficiency and Environmental Impact\",\n\"number\": \"2.a.i\",\n\"Question\": \"How have these measures impacted the overall fuel consumption and associated costs?\",\n\"context\": \"Describe the measures taken by XXXXX to improve operational efficiency in fuel usage. (Reference: 'XXXXX has consistently pursued a path of technological advancement and community-focused initiatives.')\"\n},\n{\n\"section\": \"Operational Efficiency and Environmental Impact\",\n\"number\": \"2.a.ii\",\n\"Question\": \"Provide data on the reduction in greenhouse gas emissions resulting from these efficiency improvements.\",\n\"context\": \"Describe the measures taken by XXXXX to improve operational efficiency in fuel usage. (Reference: 'XXXXX has consistently pursued a path of technological advancement and community-focused initiatives.')\"\n}, {\n\"section\": \"What technologies or innovations have been implemented to achieve these efficiencies?\",\n\"number\": \"2.a.iii\",\n\"Question\": \"Provide data on the reduction in greenhouse gas emissions resulting from these efficiency improvements.\",\n\"context\": \"Describe the measures taken by XXXXX to improve operational efficiency in fuel usage. (Reference: 'XXXXX has consistently pursued a path of technological advancement and community-focused initiatives.')\"\n}, {\n\"section\": \"LNG Terminal Operations\",\n\"number\": \"1.a.i\",\n\"Question\": \"What are the annual throughput volumes of LNG at the terminal?\",\n\"context\": \"Provide a detailed operational report on the Energía Costa Azul LNG terminal since its inception in 2008. (Reference: 'One of XXXXX’s notable achievements was the launch of the Energía Costa Azul LNG terminal in Baja California in 2008.')\"\n}, {\n\"section\": \"LNG Terminal Operations\",\n\"number\": \"1.a.ii\",\n\"Question\": \"What are the main sources of LNG supplied to the terminal, and what percentage comes from renewable sources?\",\n\"context\": \"Provide a detailed operational report on the Energía Costa Azul LNG terminal since its inception in 2008. (Reference: 'One of XXXXX’s notable achievements was the launch of the Energía Costa Azul LNG terminal in Baja California in 2008.')\"\n}, {\n\"section\": \"LNG Terminal Operations\",\n\"number\": \"1.a.iii\",\n\"Question\": \"What are the main sources of LNG supplied to the terminal, and what percentage comes from renewable sources?\",\n\"context\": \"How has the terminal's operational efficiency evolved over the years?\"\n} ] }"
                    }
                ]
                },
                {
                "role": "user",
                "content": [
                    {
                    "type": "text",
                    "text": txt_content
                    }
                ]
                }
            ],
            "temperature": 0.7,
            "top_p": 0.95,
            "max_tokens": 4026
            }  
    
    return payload

    # Send request to Azure OpenAI

def generate_prompt_url(txt_content):
    payload = {
        "messages": [
            {
            "role": "system",
            "content": [
                {
                "type": "text",
                "text": "You are an AI assistant tasked with extracting all the questions and content from the interveners who submitted questions on a regulatory document. The questions and content may be embedded within paragraphs, listed directly, or mentioned in various sections. Additionally, content may have image references like \"[Image](URL)\", make sure to extract the image URL and include in the image node.  Ensure that all questions and content are identified and listed clearly in a hierarchical order and do not include any subsections. Provide the extracted questions or content in JSON.\n"
                }
            ]
            },
            {
            "role": "user",
            "content": [
                {
                "type": "text",
                "text": "10. 1 \nAncient Discoveries and early observations of XXXXX and its electric supply \nevolution:  \n \n10. 1.1 \nStatic Electricity: Thales of Miletus discovered static electricity by \nrubbing amber with fur around 600 BCE. Electric Fish: Ancient Egyptians \nand Greeks noted the electric shocks from fish like the electric eel. \nAmber Effect: The Greeks observed that amber, when rubbed, could \nattract light objects like feathers. \n \n10. 1.1.1 \nFor each Cultural Significance and Religious Sites: Natural \nelectric phenomena were often considered divine and used in \nreligious rituals. Mythology: Stories and myths were created \naround the mysterious properties of electricity. Healing \nPractices: Electric fish were used in ancient medicine to treat \nailments like gout and headaches. \n10. 1.1.2 \nScientific Advancements and 17th and 18th Centuries William \nGilbert: Coined the term “electricus” and studied the properties of \nelectricity and magnetism. Benjamin Franklin: Conducted \nexperiments with lightning, proving it was a form of electricity. \nLeyden Jar: Invented as the first device capable of storing \nelectrical charge. \n \n\n[Image](https://bpadocumentstorage.blob.core.windows.net/images/images/image_759641774e654bd0b9c78464c93db78d.png)\nTHE XXXXX TEST DATA FOR \nDATA REQUEST QUESTIONS \nTHEIR NON-UNIFORM FORMAT OF \nQUESTIONS RECEIVED FROM THE  \nINTERVENORS \nA.2202033 \n \n10. 2 \n 19th Century Breakthroughs and Michael Faraday: Discovered electromagnetic \ninduction, leading to the development of electric motors. James Clerk Maxwell: \nFormulated the theory of electromagnetism, unifying electricity and magnetism. \n \n10.2.1 Page 5 states: “Industrial Revolution & Electrification of Cities Street \nLighting: Cities like London and New York began using electric \nstreetlights. Public Transport: Electric trams and subways \nrevolutionized urban transportation. Factories: Electricity powered \nmachinery, increasing production efficiency. Household Adoption \nAppliances: Introduction of electric appliances like refrigerators and \nwashing machines. \n \n10.2.1.1 Heating and Cooling: Electric heaters and air \nconditioners became common in homes. Communication: \nTelephones and radios became household staples, powered \nby electricity. \n \n \n \n10.2.1.2 Modern Era Technological Innovations \nSemiconductors: Development of transistors and integrated \ncircuits revolutionized electronics. Renewable Energy: \nAdvances in solar and wind power technologies. Smart Grids: \nImplementation of smart grid technology for efficient energy \ndistribution. Environmental Impact Cleaner Energy: Shift \ntowards cleaner energy sources to reduce carbon emissions. \nEnergy Efficiency: Development of energy-efficient appliances \nand lighting. Policy and Regulation: Governments \nimplementing policies to promote sustainable energy use. \nProspects & Emerging Technologies Quantum For each year, \n2022-2024, please break down forecast \n \n10.2.1.3 Computing: Potential to revolutionize computing with \nunprecedented processing power. Energy Storage: Advances \nin battery technology for better energy storage solutions. \nWireless Power: Development of wireless power transmission \ntechnologies. figures presented on page 5 by each of the \ncategories described in the quote from page 5. \n \n\nTHE XXXXX TEST DATA FOR \nDATA REQUEST QUESTIONS \nTHEIR NON-UNIFORM FORMAT OF \nQUESTIONS RECEIVED FROM THE  \nINTERVENORS \nA.2202033 \n10. 3 \nThe 21st century has witnessed a series of energy crises, beginning with the early \n2000s when oil prices surged due to geopolitical tensions in the Middle East. The \ninvasion of Iraq in 2003 and subsequent instability in the region disrupted oil \nsupplies, leading to significant price hikes. This period highlighted the world's heavy \ndependence on fossil fuels and the vulnerabilities associated with it. As economies \ngrew, the demand for energy soared, further straining the supply chains. The crisis \nunderscored the urgent need for diversifying energy sources and improving energy \nsecurity. Please provide reasons for the variation in the below graph: "
                }
            ]
            },
            {
            "role": "assistant",
            "content": [
                {
                "type": "text",
                "text": "{  \n    \"extracted_data\": [  \n        {  \n            \"section\": \"Ancient Discoveries and early observations of XXXXX and its electric supply evolution\",  \n            \"number\": \"10.1.1.1\",  \n            \"question\": \"For each Cultural Significance and Religious Sites: Natural electric phenomena were often considered divine and used in religious rituals. Mythology: Stories and myths were created around the mysterious properties of electricity. Healing Practices: Electric fish were used in ancient medicine to treat ailments like gout and headaches.\",  \n            \"context\": \"Static Electricity: Thales of Miletus discovered static electricity by rubbing amber with fur around 600 BCE. Electric Fish: Ancient Egyptians and Greeks noted the electric shocks from fish like the electric eel. Amber Effect: The Greeks observed that amber, when rubbed, could attract light objects like feathers.\"  \n        },  \n        {  \n            \"section\": \"Ancient Discoveries and early observations of XXXXX and its electric supply evolution\",  \n            \"number\": \"10.1.1.2\",  \n            \"question\": \"Scientific Advancements and 17th and 18th Centuries William Gilbert: Coined the term “electricus” and studied the properties of electricity and magnetism. Benjamin Franklin: Conducted experiments with lightning, proving it was a form of electricity. Leyden Jar: Invented as the first device capable of storing electrical charge.\",  \n            \"image\": \"https://bpadocumentstorage.blob.core.windows.net/images/images/image_13de73d5935f485db77a3122a185c1f1.png\",\n            \"context\": \"Static Electricity: Thales of Miletus discovered static electricity by rubbing amber with fur around 600 BCE. Electric Fish: Ancient Egyptians and Greeks noted the electric shocks from fish like the electric eel. Amber Effect: The Greeks observed that amber, when rubbed, could attract light objects like feathers.\"  \n        },    \n        {  \n            \"section\": \"19th Century Breakthroughs and Michael Faraday: Discovered electromagnetic induction, leading to the development of electric motors. James Clerk Maxwell: Formulated the theory of electromagnetism, unifying electricity and magnetism.\",  \n            \"number\": \"10.2.1.1\",  \n            \"Question\": \"Heating and Cooling: Electric heaters and airconditioners became common in homes. Communication:Telephones and radios became household staples, poweredby electricity.\",  \n            \"context\": \"Page 5 states: “Industrial Revolution & Electrification of Cities Street Lighting: Cities like London and New York began using electric streetlights. Public Transport: Electric trams and subways revolutionized urban transportation. Factories: Electricity powered machinery, increasing production efficiency. Household Adoption Appliances: Introduction of electric appliances like refrigerators and washing machines.\"  \n        },  \n        {  \n            \"section\": \"19th Century Breakthroughs and Michael Faraday: Discovered electromagnetic induction, leading to the development of electric motors. James Clerk Maxwell: Formulated the theory of electromagnetism, unifying electricity and magnetism.\",  \n            \"number\": \"10.2.1.2\",  \n            \"question\": \"Modern Era Technological Innovations Semiconductors: Development of transistors and integrated circuits revolutionized electronics. Renewable Energy: Advances in solar and wind power technologies. Smart Grids: Implementation of smart grid technology for efficient energy distribution. Environmental Impact Cleaner Energy: Shift towards cleaner energy sources to reduce carbon emissions. Energy Efficiency: Development of energy-efficient appliances and lighting. Policy and Regulation: Governments implementing policies to promote sustainable energy use. Prospects & Emerging Technologies Quantum For each year, 2022-2024, please break down forecast\",  \n            \"context\": \"Page 5 states: “Industrial Revolution & Electrification of Cities Street Lighting: Cities like London and New York began using electric streetlights. Public Transport: Electric trams and subways revolutionized urban transportation. Factories: Electricity powered machinery, increasing production efficiency. Household Adoption Appliances: Introduction of electric appliances like refrigerators and washing machines.\"  \n        },  \n        {  \n            \"section\": \"19th Century Breakthroughs and Michael Faraday: Discovered electromagnetic induction, leading to the development of electric motors. James Clerk Maxwell: Formulated the theory of electromagnetism, unifying electricity and magnetism.\",  \n            \"number\": \"10.2.1.3\",  \n            \"question\": \"Computing: Potential to revolutionize computing with unprecedented processing power. Energy Storage: Advances in battery technology for better energy storage solutions.Wireless Power: Development of wireless power transmission technologies. figures presented on page 5 by each of the categories described in the quote from page 5\",  \n            \"context\": \"Page 5 states: “Industrial Revolution & Electrification of Cities Street Lighting: Cities like London and New York began using electric streetlights. Public Transport: Electric trams and subways revolutionized urban transportation. Factories: Electricity powered machinery, increasing production efficiency. Household Adoption Appliances: Introduction of electric appliances like refrigerators and washing machines.\"  \n        },  \n        {  \n            \"section_name\": \"10.2.1.3 Prospects & Emerging Technologies\",  \n            \"number\": \"7\",  \n            \"text\": \"- Quantum Computing: Potential to revolutionize computing with unprecedented processing power.\\n- Energy Storage: Advances in battery technology for better energy storage solutions.\\n- Wireless Power: Development of wireless power transmission technologies.\",  \n            \"context\": \"Emerging technologies in the energy sector.\"  \n        },  \n        {  \n            \"section_name\": \"\",  \n            \"number\": \"10.3\",  \n            \"text\": \"The 21st century has witnessed a series of energy crises, beginning with the early 2000s when oil prices surged due to geopolitical tensions in the Middle East. The invasion of Iraq in 2003 and subsequent instability in the region disrupted oil supplies, leading to significant price hikes. This period highlighted the world's heavy dependence on fossil fuels and the vulnerabilities associated with it. As economies grew, the demand for energy soared, further straining the supply chains. The crisis underscored the urgent need for diversifying energy sources and improving energy security. Please provide reasons for the variation in the below graph:\",\n            \"image\": \"https://bpadocumentstorage.blob.core.windows.net/images/images/image_e13f2b39bb374146a36bd4ebcfe36ab0.png\",\n            \"context\": \"\"  \n        }  \n    ]  \n}"
                }
            ]
            },
            {
                "role": "user",
                "content": [
                    {
                    "type": "text",
                    "text": txt_content
                    }
                ]
            }
        ],
        "temperature": 0.7,
        "top_p": 0.95,
        "max_tokens": 4026
        }
    
    return payload
      
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
