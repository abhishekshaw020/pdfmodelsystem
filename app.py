import logging
import os

from cachetools import Cache
import pdfplumber
import requests
from bs4 import BeautifulSoup
from celery import Celery
from flask import Flask, request, jsonify, render_template
from flask_caching import Cache
from transformers import pipeline

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200 MB limit
app.config['CELERY_BROKER_URL'] = 'redis://localhost:6379/0'
app.config['CELERY_RESULT_BACKEND'] = 'redis://localhost:6379/0'
app.config['CACHE_TYPE'] = 'simple'

cache = Cache(app)
celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
celery.conf.update(app.config)

# Ensure the upload folder exists
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

# Initialize the question-answering pipeline with an explicit model
try:
    qa_pipeline = pipeline("question-answering", model="distilbert-base-cased-distilled-squad")
except Exception as e:
    logging.error(f"Error initializing QA pipeline: {e}")
    qa_pipeline = None


@celery.task
def extract_text_from_pdf_task(pdf_file_path):
    text = ""
    try:
        with pdfplumber.open(pdf_file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text
    except Exception as e:
        logging.error(f"Error extracting text from PDF: {e}")
        return None
    return text


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload_pdf():
    try:
        if 'pdf' not in request.files:
            return jsonify({"error": "No file part"}), 400
        file = request.files['pdf']
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400
        if file and file.filename.endswith('.pdf'):
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
            file.save(file_path)
            task = extract_text_from_pdf_task.delay(file_path)
            return jsonify({"task_id": task.id}), 202
        else:
            return jsonify({"error": "Invalid file type"}), 400
    except Exception as e:
        logging.error(f"Error during file upload: {e}")
        return jsonify({"error": "Internal server error"}), 500


@app.route('/task_status/<task_id>', methods=['GET'])
def task_status(task_id):
    task = extract_text_from_pdf_task.AsyncResult(task_id)
    if task.state == 'PENDING':
        response = {
            'state': task.state,
            'current': 0,
            'total': 1,
            'status': 'Pending...'
        }
    elif task.state != 'FAILURE':
        response = {
            'state': task.state,
            'current': 1,
            'total': 1,
            'status': 'Completed!',
            'result': task.result
        }
    else:
        response = {
            'state': task.state,
            'current': 1,
            'total': 1,
            'status': str(task.info),
        }
    return jsonify(response)


@app.route('/ask', methods=['POST'])
def ask_question():
    if qa_pipeline is None:
        return jsonify({"error": "QA pipeline not available"}), 500
    try:
        data = request.json
        question = data.get('question', '')
        text = data.get('text', '')

        if not question or not text:
            return jsonify({"error": "Invalid input"}), 400

        # Answer from PDF content
        answer = qa_pipeline(question=question, context=text)
        if answer['score'] > 0.1:  # If confidence is sufficient
            return jsonify({"answer": answer['answer']})

        # Fallback to internet search
        search_results = search_internet(question)
        return jsonify({"answer": "Information not found in the PDF. Here are some web search results.",
                        "search_results": search_results})
    except Exception as e:
        logging.error(f"Error during question answering: {e}")
        return jsonify({"error": "Internal server error"}), 500


def search_internet(query):
    search_url = f"https://www.google.com/search?q={query}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    response = requests.get(search_url, headers=headers)
    soup = BeautifulSoup(response.text, 'html.parser')
    results = []
    for g in soup.find_all('div', class_='tF2Cxc'):
        title = g.find('h3').text
        link = g.find('a')['href']
        snippet = g.find('span').text
        results.append({"title": title, "link": link, "snippet": snippet})
    return results


@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"error": "File too large"}), 413


@app.errorhandler(404)
def page_not_found(error):
    return jsonify({"error": "Page not found"}), 404


@app.errorhandler(500)
def internal_server_error(error):
    return jsonify({"error": "Internal server error"}), 500


if __name__ == '__main__':
    # Set up logging to output to the console
    logging.basicConfig(level=logging.DEBUG)
    app.run(debug=False)
