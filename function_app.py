#This Sample Code is provided for the purpose of illustration only and is not intended to be used in a production environment. THIS SAMPLE CODE AND ANY RELATED INFORMATION ARE PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND, EITHER EXPRESSED OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE IMPLIED WARRANTIES OF MERCHANTABILITY AND/OR FITNESS FOR A PARTICULAR PURPOSE. We grant You a nonexclusive, royalty-free right to use and modify the Sample Code and to reproduce and distribute the object code its authors,or anyone else involved in the creation, production, or delivery of the scripts be liable for any damages whatsoever (including, without limitation, damages for loss of business profits, business interruption, loss of business information, or other pecuniary loss) arising out of the use of or inability to use the sample scripts or documentation, even if its has been advised of the possibility of such damages" and keep the repo with MIT license

import azure.functions as func
import logging
import os
from azure.storage.blob import BlobServiceClient
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeResult, AnalyzeOutputOption, AnalyzeDocumentRequest
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
import openai
import prompty
import prompty.azure
from azure.cosmos import CosmosClient
import uuid
import datetime

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

blob_service_client = BlobServiceClient.from_connection_string(
            os.environ['AzureWebJobsStorage']
        )

cosmos_connection_string = os.environ['COSMOSDB_CONNECTION_STRING']
cosmos_client = CosmosClient.from_connection_string(cosmos_connection_string)

# Get reference to the database and container
database_name = 'sempradocumentcontent'
container_name = 'auditrail'

@app.route(route="regulatorydocumentprocessing")
def regulatorydocumentprocessing(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function processed a request.')

    documentname = req.params.get('documentname')
    download = None
    responsecontent = None
    analyzed_content = None
    document_url = None

    if documentname:
        try:
            download = download_blob("documents", documentname)
            analyzed_content = analyze_document(documentname, download['document_content'])
            #content = call_openai_api(content).choices[0].message.content
            responsecontent = call_prompty(documentname, analyzed_content)
        except Exception as e:
            logging.error(f"Error occurred while processing the document: {e}")
            return func.HttpResponse(
                "Error occurred while processing the document. Please check the logs for more details.",
                status_code=500)
        
        # Store audit data in Cosmos DB
        audit_log = {
            'id': str(uuid.uuid4()),
            'document_name': documentname,
            'status': "success",
            'message': {"content": "Document processed successfully"},
            'timestamp': datetime.datetime.utcnow().isoformat(),
            'document_url': download['url'],
            'analyzed_content': analyzed_content,
            'response_content': responsecontent
        }

        store_audit_in_cosmos(audit_log)

        return func.HttpResponse(
                    responsecontent,
                    mimetype="application/json",
                    status_code=200
                )
    else:
        logging.error('No document name provided')
        return func.HttpResponse(
             "This HTTP triggered function executed successfully. Pass a name in the query string or in the request body for a personalized response.",
             status_code=200
        )

def call_openai_api(txt_content):
    endpoint = os.environ['OPENAI_ENDPOINT']
    openai_key = os.environ['OPENAI_KEY']
    prompt = generate_prompt(txt_content)

    client = openai.AzureOpenAI(
        api_key=openai_key,
        api_version="2024-02-15-preview",
        azure_endpoint = endpoint
    )

    # Create and return a new chat completion request
    return client.chat.completions.create(
        model="gpt-4o-mini",
        messages=prompt['messages'],
        stream=False
    )    

def call_prompty(document_name, txt_content):
    regulatoryprompt = "intervenor2.prompty"

    if document_name == "Intervenor1_Data Request Template.pdf":
        regulatoryprompt = "intervenor1.prompty"
    
    configuration = {
    "type": "azure_openai",
    "azure_endpoint": os.environ['OPENAI_ENDPOINT'],
    "api_key": os.environ['OPENAI_KEY'],
    "api_version": "2024-02-15-preview",
    "azure_deployment": "gpt-4o-mini"}

    response = prompty.execute(regulatoryprompt, inputs={"extractedcontent": txt_content}, configuration=configuration)
    return response

def generate_prompt(txt_content):
    payload = {
        "messages": [
            {
            "role": "system",
            "content": [
                {
                "type": "text",
                "text": "You are an AI assistant that understands and extract questions from a regulatory document submitted by an intervenor. Include Images given in the questions.  Do not skip the questions even if there are no nested sub questions, rather skip the nested sub question nodes when there are no sub questions under main question."
                }
            ]
            },
            {
            "role": "user",
            "content": [
                {
                "type": "text",
                "text": "Sempra NA INFRASTRUCTURE\nSempra, a leading North American energy infrastructure company, has a rich history of innovation and\ngrowth. Formed in 1998 through the merger of Pacific Enterprises and Enova Corporation, Sempra has\nconsistently pursued a path of technological advancement and community-focused initiatives.\nSempra's Journey of Innovation and Growth\nTo:\nJohn Ivy, on behalf of Sempra, Jivy@sdge.com\nJohn Appeased, on behalf of SCG, JAPP.com\nDate Sent:\nSeptember 19, 2035\nResponse Due: October 3, 2045\nPlease provide a response to the following: Sempra, a leading North American energy infrastructure\ncompany, has a rich history of innovation and growth. Formed in 1998 through the merger of Pacific\nEnterprises and Enova Corporation, Sempra has consistently pursued a path of technological\nadvancement and community-focused initiatives.\nFrom its inception, Sempra aimed to deliver energy differently. The company quickly became a\nsignificant player in the energy sector, serving millions of consumers across Southern California. Over\nthe years, Sempra expanded its reach and capabilities, acquiring key assets and investing in renewable\nenergy projects.\nPlease note that the questions in this data request relate to both SoCalGas and SDG&E.\nGENERAL INSTRUCTIONS\n1.\nOne of Sempra's notable achievements was the launch of the Energia Costa Azul LNG terminal\nin Baja California in 2008, one of the first liquefied natural gas (LNG) receipt terminals on the West\nCoast of North America. This project marked a significant milestone in Sempra's commitment to\nproviding reliable and clean energy solutions.\n2.\nTwo of Sempra's notable achievements was the launch of the Energía Costa Azul LNG terminal\nin Baja California in 2008, one of the first liquefied natural gas (LNG) receipt terminals on the West\nCoast of North America. This project marked a significant milestone in Sempra's commitment to\nproviding reliable and clean energy solutions.\n3.\nThree of One of Sempra's notable achievements was the launch of the Energía Costa Azul LNG\nterminal in Baja California in 2008, one of the first liquefied natural gas (LNG) receipt terminals on the\nWest Coast of North America. This project marked a significant milestone in Sempra's commitment to\nproviding reliable and clean energy solutions.\n4.\nFour of One of Sempra's notable achievements was the launch of the Energía Costa Azul LNG\nterminal in Baja California in 2008, one of the first liquefied natural gas (LNG) receipt terminals on the\nWest Coast of North America. This project marked a significant milestone in Sempra's commitment to\nproviding reliable and clean energy solutions.\n5.\nResponses to these data requests should be transmitted as they become available.\nDEFINITIONS\nA. ASSETS: Oil and gas companies often have extensive assets, including oil fields, refineries,\npipelines, and storage facilities.\nB. AMERICAN GIANTS: Major U.S .- based oil companies include ExxonMobil and Chevron, both of\nwhich are leaders in exploration, production, and refining.\nC. ACQUISITIONS: The industry frequently sees mergers and acquisitions, as companies aim to\nexpand their reserves and market share.\nD. ADVANCEMENTS: Technological advancements, such as hydraulic fracturing and deep-water\ndrilling, have significantly increased oil and gas production capabilities.\nE. ALTERNATIVE ENERGY: Many oil and gas companies are investing in alternative energy\nsources, including wind, solar, and hydrogen, to diversify their energy portfolios.\nF. ASIA: Companies like PetroChina and Sinopec are major players in the Asian market,\ncontributing significantly to global oil and gas production.\nG. ANALYSIS: Detailed geological and seismic analysis is crucial for identifying potential oil and\ngas reserves.\nH. AFFILIATES: Large oil companies often have numerous affiliates and subsidiaries involved in\nvarious aspects of the energy sector.\nI. AGRE\nJ. EMENTS: International agreements and partnerships are common, allowing companies to\nexplore and produce oil and gas in different regions.\nK. AUTOMATION: The use of automation and digital technologies is increasing in the oil and gas\nindustry to improve efficiency and safety.\nL. ALLOCATIONS: Capital allocation strategies are critical for oil and gas companies to balance\ninvestments in traditional and renewable energy projects.\nTEST SET OF DATA REQUESTS\nFor Fuel\nGiven Sempra's historical emphasis on both traditional and renewable energy sources, it is essential to\nscrutinize the financial and operational aspects related to fuel procurement and distribution.\nUnderstanding these metrics will help evaluate the cost-effectiveness and sustainability of Sempra's\nfuel-related operations.\n1. Fuel Procurement and Cost Analysis\na) Please provide a detailed breakdown of the cost structure associated with fuel\nprocurement over the past decade, specifically distinguishing between traditional fossil\nfuels and renewable energy sources. (Reference: \"Sempra expanded its reach and\ncapabilities, acquiring key assets and investing in renewable energy projects.\")\ni.\nWhat percentage of the total fuel procurement budget was allocated to\nrenewable energy sources each year?\nii.\nHow has the cost per unit of fuel evolved over the years for both traditional and\nrenewable sources?\niii.\nWhat suppliers and partners have been involved in the procurement process for\nboth types of fuel?\n2. Operational Efficiency and Environmental Impact\na) Describe the measures taken by Sempra to improve operational efficiency in fuel usage.\n(Reference: \"Sempra has consistently pursued a path of technological advancement and\ncommunity-focused initiatives.\")\ni.\nHow have these measures impacted the overall fuel consumption and\nassociated costs?\nii.\nProvide data on the reduction in greenhouse gas emissions resulting from these\nefficiency improvements.\niii.\nWhat technologies or innovations have been implemented to achieve these\nefficiencies?\n......\n....."
                }
            ]
            },
            {
            "role": "assistant",
            "content": [
                {
                "type": "text",
                "text": "{\n  \"data\": [\n    {\n      \"context\": \"Given Sempra's historical emphasis on both traditional and renewable energy sources, it is essential to scrutinize the financial and operational aspects related to fuel procurement and distribution. Understanding these metrics will help evaluate the cost-effectiveness and sustainability of Sempra's fuel-related operations.\",\n      \"questions\": [\n        {\n          \"mainquestion\": \"1.\tFuel Procurement and Cost Analysis\",\n          \"nested1subquestion1\": \"a) Please provide a detailed breakdown of the cost structure associated with fuel procurement over the past decade, specifically distinguishing between traditional fossil fuels and renewable energy sources. (Reference: 'Sempra expanded its reach and capabilities, acquiring key assets and investing in renewable energy projects.')\",\n          \"subquestions\": [\n            {\n              \"nested2subquestion1\": \"i. What percentage of the total fuel procurement budget was allocated to renewable energy sources each year?\"\n            },\n            {\n              \"nested2subquestion2\": \"ii. How has the cost per unit of fuel evolved over the years for both traditional and renewable sources?\"\n            },\n            {\n              \"nested2subquestion3\": \"iii. What suppliers and partners have been involved in the procurement process for both types of fuel?\"\n            }\n          ]\n        }\n      ]\n    },\n    {\n      \"context\": \"Given Sempra's historical emphasis on both traditional and renewable energy sources, it is essential to scrutinize the financial and operational aspects related to fuel procurement and distribution. Understanding these metrics will help evaluate the cost-effectiveness and sustainability of Sempra's fuel-related operations.\",\n      \"questions\": [\n        {\n          \"mainquestion\": \"2. Operational Efficiency and Environmental Impact\",\n          \"nested1subquestion1\": \"a)\tDescribe the measures taken by Sempra to improve operational efficiency in fuel usage. (Reference: 'Sempra has consistently pursued a path of technological advancement and community-focused initiatives.')\",\n          \"subquestions\": [\n            {\n              \"nested2subquestion1\": \"i.\tHow have these measures impacted the overall fuel consumption and associated costs?\"\n            },\n            {\n              \"nested2subquestion2\": \"ii.\tProvide data on the reduction in greenhouse gas emissions resulting from these efficiency improvements.\"\n            },\n            {\n              \"nested2subquestions3\": \"iii. What technologies or innovations have been implemented to achieve these efficiencies?\"\n            }\n          ]\n        }\n      ]\n    }\n  ]\n}"
                }
            ]
            },
            {
            "role": "user",
            "content": [
                {
                "type": "text",
                "text": "THE SEMPRA TEST DATA FOR\nDATA REQUEST QUESTIONS\nTHEIR NON-UNIFORM FORMAT OF\nQUESTIONS RECEIVED FROM THE\nINTERVENORS\nA.2202033\n10. 1\nAncient Discoveries and early observations of sempra and its electric supply\nevolution:\n10. 1.1\nStatic Electricity: Thales of Miletus discovered static electricity by\nrubbing amber with fur around 600 BCE. Electric Fish: Ancient Egyptians\nand Greeks noted the electric shocks from fish like the electric eel.\nAmber Effect: The Greeks observed that amber, when rubbed, could\nattract light objects like feathers.\n10. 1.1.1\nFor each Cultural Significance and Religious Sites: Natural\nelectric phenomena were often considered divine and used in\nreligious rituals. Mythology: Stories and myths were created\naround the mysterious properties of electricity. Healing\nPractices: Electric fish were used in ancient medicine to treat\nailments like gout and headaches.\n10. 1.1.2\nScientific Advancements and 17th and 18th Centuries William\nGilbert: Coined the term \"electricus\" and studied the properties of\nelectricity and magnetism. Benjamin Franklin: Conducted\nexperiments with lightning, proving it was a form of electricity.\nLeyden Jar: Invented as the first device capable of storing\nelectrical charge.\nCountry\nEnergy Consumption Increase (%)\nUSA\n15\nChina\n25\nIndia\n20\nRussia\n10\nJapan\n8\nGermany\n7\nBrazil\n5\nCanada\n4\nFrance\n3\nUK\n3\nThis table shows the percentage increase in energy consumption by various countries over the last five years.\n\nTHE SEMPRA TEST DATA FOR\nDATA REQUEST QUESTIONS\nTHEIR NON-UNIFORM FORMAT OF\nQUESTIONS RECEIVED FROM THE\nINTERVENORS\nA.2202033\n10.2\n19th Century Breakthroughs and Michael Faraday: Discovered electromagnetic\ninduction, leading to the development of electric motors. James Clerk Maxwell:\nFormulated the theory of electromagnetism, unifying electricity and magnetism.\n10.2.1 Page 5 states: \"Industrial Revolution & Electrification of Cities Street\nLighting: Cities like London and New York began using electric\nstreetlights. Public Transport: Electric trams and subways\nrevolutionized urban transportation. Factories: Electricity powered\nmachinery, increasing production efficiency. Household Adoption\nAppliances: Introduction of electric appliances like refrigerators and\nwashing machines.\n10.2.1.1 Heating and Cooling: Electric heaters and air\nconditioners became common in homes. Communication:\nTelephones and radios became household staples, powered\nby electricity.\n10.2.1.2 Modern Era Technological Innovations\nSemiconductors: Development of transistors and integrated\ncircuits revolutionized electronics. Renewable Energy:\nAdvances in solar and wind power technologies. Smart Grids:\nImplementation of smart grid technology for efficient energy\ndistribution. Environmental Impact Cleaner Energy: Shift\ntowards cleaner energy sources to reduce carbon emissions.\nEnergy Efficiency: Development of energy-efficient appliances\nand lighting. Policy and Regulation: Governments\nimplementing policies to promote sustainable energy use.\nProspects & Emerging Technologies Quantum For each year,\n2022-2024, please break down forecast\n10.2.1.3 Computing: Potential to revolutionize computing with\nunprecedented processing power. Energy Storage: Advances\nin battery technology for better energy storage solutions.\nWireless Power: Development of wireless power transmission\ntechnologies. figures presented on page 5 by each of the\ncategories described in the quote from page 5.\nTHE SEMPRA TEST DATA FOR\nDATA REQUEST QUESTIONS\nTHEIR NON-UNIFORM FORMAT OF\nQUESTIONS RECEIVED FROM THE\nINTERVENORS\nA.2202033\n10.3\nThe 21st century has witnessed a series of energy crises, beginning with the early\n2000s when oil prices surged due to geopolitical tensions in the Middle East. The\ninvasion of Iraq in 2003 and subsequent instability in the region disrupted oil\nsupplies, leading to significant price hikes. This period highlighted the world's heavy\ndependence on fossil fuels and the vulnerabilities associated with it. As economies\ngrew, the demand for energy soared, further straining the supply chains. The crisis\nunderscored the urgent need for diversifying energy sources and improving energy\nsecurity. Please provide reasons for the variation in the below graph:\nAnnual Energy Consumption Increase by Countries (Last 5 Years)\nImage URL: https://sempradocumentprocessing.blob.core.windows.net/images/Intervenor2_Data%20Request%20Template.pdf_3.1.png\n25\nEnergy Consumption Increase (%)\n20\n15\n10\n5\n0\nUSA\nChina\nIndia\nRussia\nJapan\nGermany\nBrazil\nCanada\nFrance\nUK\nCountries"
                }
            ]
            },
            {
            "role": "assistant",
            "content": [
                {
                "type": "text",
                "text": "{\n    \"data\": [\n      {\n        \"questions\": [\n          {\n            \"mainquestion\": \"10. 1 Ancient Discoveries and early observations of sempra and its electric supply evolution:\",\n            \"nested1subquestion1\": \"10. 1.1 Static Electricity: Thales of Miletus discovered static electricity by rubbing amber with fur around 600 BCE. Electric Fish: Ancient Egyptians and Greeks noted the electric shocks from fish like the electric eel. Amber Effect: The Greeks observed that amber, when rubbed, could attract light objects like feathers.\",\n            \"subquestions\": [\n              {\n                \"nested2subquestion1\": \"10. 1.1.1 For each Cultural Significance and Religious Sites: Natural electric phenomena were often considered divine and used in religious rituals. Mythology: Stories and myths were created around the mysterious properties of electricity. Healing Practices: Electric fish were used in ancient medicine to treat ailments like gout and headaches.\"\n              },\n              {\n                \"nested2subquestion2\": \"10. 1.1.2 Scientific Advancements and 17th and 18th Centuries William Gilbert: Coined the term 'electricus' and studied the properties of electricity and magnetism. Benjamin Franklin: Conducted experiments with lightning, proving it was a form of electricity. Leyden Jar: Invented as the first device capable of storing electrical charge.  This table shows the percentage increase in energy consumption by various countries over the last five years.\"\n              }\n            ]\n          }\n        ]\n      },\n      {\n        \"questions\": [\n          {\n            \"mainquestion\": \"10.2 19th Century Breakthroughs and Michael Faraday: Discovered electromagnetic induction, leading to the development of electric motors. James Clerk Maxwell: Formulated the theory of electromagnetism, unifying electricity and magnetism.\",\n            \"nested1subquestion1\": \"10.2.1 Page 5 states: 'Industrial Revolution & Electrification of Cities Street Lighting: Cities like London and New York began using electric streetlights. Public Transport: Electric trams and subways revolutionized urban transportation. Factories: Electricity powered machinery, increasing production efficiency. Household Adoption Appliances: Introduction of electric appliances like refrigerators and washing machines.\",\n            \"subquestions\": [\n              {\n                \"nested2subquestion1\": \"10.2.1.1 Heating and Cooling: Electric heaters and airvconditioners became common in homes. Communication:vTelephones and radios became householdvstaples, poweredvby electricity.\"\n              },\n              {\n                \"nested2subquestion2\": \"10.2.1.2 Modern Era Technological Innovations Semiconductors: Development of transistors and integrated circuits revolutionized electronics. Renewabl Energy: Advances in solar and wind power technologies. Smart Grids: Implementation of smart grid technology for efficient energy distribution. Environmental Impact Cleaner Energy: Shift towards cleaner energy sources to reduce carbon emissions. Energy Efficiency: Development of energy-efficient appliances and lighting. Policy and Regulation: Governments implementing policies to promote sustainable energy use. Prospects & Emerging Technologies Quantum For each year, 2022-2024, please break down forecast\"\n              },\n              {\n                \"nested2subquestions3\": \"10.2.1.3 Computing: Potential to revolutionize computing with unprecedented processing power. Energy Storage: Advancesin battery technology for better energy storage solutions. Wireless Power: Development of wireless power transmission technologies. figures presented on page 5 by each of the categories described in the quote from page 5.\"\n              }\n            ]\n          }\n        ]\n      },\n      {\n        \"questions\": [\n          {\n            \"mainquestion\": \"10.3 The 21st century has witnessed a series of energy crises, beginning with the early 2000s when oil prices surged due to geopolitical tensions in the Middle East. The invasion of Iraq in 2003 and subsequent instability in the region disrupted oil supplies, leading to significant price hikes. This period highlighted the world's heavy dependence on fossil fuels and the vulnerabilities associated with it. As economies grew, the demand for energy soared, further straining the supply chains. The crisis underscored the urgent need for diversifying energy sources and improving energy security. Please provide reasons for the variation in the below graph: Annual Energy Consumption Increase by Countries (Last 5 Years) Image URL: https://sempradocumentprocessing.blob.core.windows.net/images/Intervenor2_Data%20Request%20Template.pdf_3.1.png\"\n          }\n        ]\n      }\n    ]\n  }"
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
        "temperature": 0.1,
        "top_p": 0.95,
        "max_tokens": 11576
        }    
    
    return payload
    
def download_blob(container_name, blob_name):
    document_url = None
    try:
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
        blob_data = blob_client.download_blob()
        document_content = blob_data.readall()
        document_url = blob_client.url

        logging.info(f"Document downloaded successfully: {blob_client.url}")
    except Exception as e:
        logging.error(f"Error occurred while downloading the document: {e}")
        raise e
    
    return {"url": document_url, "document_content": document_content}

def analyze_document(document_name, document_content):
    diendpoint = os.environ['DOCUMENT_INTELLIGENCE_ENDPOINT']
    dikey = os.environ['DOCUMENT_INTELLIGENCE_KEY']

    document_intelligence_client = DocumentIntelligenceClient(
        endpoint=diendpoint, credential=AzureKeyCredential(dikey)
        )
    
    poller = document_intelligence_client.begin_analyze_document(
            "prebuilt-layout", analyze_request=AnalyzeDocumentRequest(bytes_source=document_content), output=[AnalyzeOutputOption.FIGURES]
        )
    
    result: AnalyzeResult = poller.result()
    operation_id = poller.details["operation_id"]

    page_table = process_tables(result.tables)

    page_figures = process_image(result.figures, document_intelligence_client, result, operation_id, document_name)
    
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
                
                if line_text=="This table shows the percentage increase in energy consumption by various countries over the last five years.":
                    extracted_content += page_table

                extracted_content += line_text + "\n"
                
                # Check if the line matches any figure caption
                if line_text in caption_to_image:
                    # Insert the image URL after the caption
                    image_url = caption_to_image[line_text]
                    extracted_content += f"Image URL: {image_url}\n"
        else:
            logging.info(f"No text content found on page {page_number}.")
    
    print(f"Extracted text from document '{document_name}' with images inserted.")

    logging.info(f"Document analyzed successfully")
    return extracted_content

def process_tables(tables):
 
    table_content = ""
    for table_idx, table in enumerate(tables):
        table_content += f"\nTable #{table_idx + 1}\n"
        max_row = table.row_count
        max_col = table.column_count
        
        # Initialize a 2D list to store cell contents
        cells = [["" for _ in range(max_col)] for _ in range(max_row)]
        for cell in table.cells:
            cells[cell.row_index][cell.column_index] = cell.content.strip()
        
        # Build the table string with borders
        for row in cells:
            row_content = "| " + " | ".join(row) + " |"
            table_content += row_content + "\n"
            table_content += "|---" * max_col + "|\n"
    return table_content

def process_image(figures, document_intelligence_client, result, operation_id, document_name):
    page_figures = {}

    if figures:
        for figure in figures:
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
    
    return page_figures

def store_audit_in_cosmos(audit_data):
    try:
        # Initialize Cosmos DB client using connection string
        cosmos_connection_string = os.environ['COSMOSDB_CONNECTION_STRING']
        cosmos_client = CosmosClient.from_connection_string(cosmos_connection_string)
        
        # Reference to the database and container
        database_name = 'sempradocumentcontent'
        container_name = 'auditrail'
        
        database = cosmos_client.get_database_client(database_name)
        container = database.get_container_client(container_name)
        
        # Create a new item in the container
        container.create_item(body=audit_data)
        logging.info('Audit data stored successfully in Cosmos DB.')
    except Exception as e:
        logging.error(f"An error occurred while storing audit data: {e}")