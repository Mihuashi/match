from elasticsearch import Elasticsearch
from flask import Flask, request
from image_match.elasticsearch_driver import SignatureES
from image_match.goldberg import ImageSignature
import json
import os
import sys

# =============================================================================
# 使用 FixSignatureES 替换 SignatureES 以修复 search_single_record 方法

from image_match.signature_database_base import normalized_distance
import numpy as np

class FixSignatureES(SignatureES):
    def search_single_record(self, rec):
        path = rec.pop('path')
        signature = rec.pop('signature')
        if 'metadata' in rec:
            rec.pop('metadata')

        # build the 'should' list
        should = [{'term': {word: rec[word]}} for word in rec]
        res = self.es.search(index=self.index,
                            # Elasticsearch Serverless 的 {index}/{type}/_search (过时)接口不支持 size 参数，所以需要这里去掉，改用 {index}/_search 接口
                            #   doc_type=self.doc_type,
                              body={'query': {
                                       'bool': {'should': should}
                                     },
                                    '_source': {'excludes': ['simple_word_*']}
                                   },
                              size=self.size,
                              timeout=self.timeout)['hits']['hits']

        sigs = np.array([x['_source']['signature'] for x in res])

        if sigs.size == 0:
            return []

        dists = normalized_distance(sigs, np.array(signature))

        formatted_res = [{'id': x['_id'],
                          'score': x['_score'],
                          'metadata': x['_source'].get('metadata'),
                          'path': x['_source'].get('url', x['_source'].get('path'))}
                         for x in res]

        for i, row in enumerate(formatted_res):
            row['dist'] = dists[i]
        formatted_res = filter(lambda y: y['dist'] < self.distance_cutoff, formatted_res)

        return formatted_res

# =============================================================================
# Globals

es_host = os.environ['ELASTIC_HOST']
es_user = os.environ['ELASTIC_USER']
es_password = os.environ['ELASTIC_PASSWORD']
es_index = os.environ['ELASTICSEARCH_INDEX']
es_doc_type = os.environ['ELASTICSEARCH_DOC_TYPE']
all_orientations = os.environ['ALL_ORIENTATIONS']

app = Flask(__name__)
es = Elasticsearch([es_host], verify_certs=True, timeout=60, max_retries=10, retry_on_timeout=True, http_auth=(es_user, es_password))
ses = FixSignatureES(es, index=es_index, doc_type=es_doc_type)
gis = ImageSignature()

# Try to create the index and ignore IndexAlreadyExistsException
# if the index already exists
es.indices.create(index=es_index, ignore=400)

# =============================================================================
# Helpers

def ids_with_path(path):
    matches = es.search(index=es_index,
                        _source='_id',
                        q='path:' + json.dumps(path))
    return [m['_id'] for m in matches['hits']['hits']]

def paths_at_location(offset, limit):
    search = es.search(index=es_index,
                       from_=offset,
                       size=limit,
                       _source='path')
    return [h['_source']['path'] for h in search['hits']['hits']]

def count_images():
    return es.count(index=es_index)['count']

def delete_ids(ids):
    for i in ids:
        es.delete(index=es_index, doc_type=es_doc_type, id=i, ignore=404)

def dist_to_percent(dist):
    return (1 - dist) * 100

def get_image(url_field, file_field):
    if url_field in request.form:
        return request.form[url_field], False
    else:
        return request.files[file_field].read(), True

# =============================================================================
# Routes

@app.route('/add', methods=['POST'])
def add_handler():
    path = request.form['filepath']
    try:
        metadata = json.loads(request.form['metadata'])
    except KeyError:
        metadata = None
    img, bs = get_image('url', 'image')

    old_ids = ids_with_path(path)
    ses.add_image(path, img, bytestream=bs, metadata=metadata)
    delete_ids(old_ids)

    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'add',
        'result': []
    })

@app.route('/delete', methods=['DELETE'])
def delete_handler():
    path = request.form['filepath']
    ids = ids_with_path(path)
    delete_ids(ids)
    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'delete',
        'result': []
    })

@app.route('/search', methods=['POST'])
def search_handler():
    img, bs = get_image('url', 'image')
    ao = request.form.get('all_orientations', all_orientations) == 'true'

    matches = ses.search_image(
            path=img,
            all_orientations=ao,
            bytestream=bs)

    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'search',
        'result': [{
            'score': dist_to_percent(m['dist']),
            'filepath': m['path'],
            'metadata': m['metadata']
        } for m in matches]
    })

@app.route('/compare', methods=['POST'])
def compare_handler():
    img1, bs1 = get_image('url1', 'image1')
    img2, bs2 = get_image('url2', 'image2')
    img1_sig = gis.generate_signature(img1, bytestream=bs1)
    img2_sig = gis.generate_signature(img2, bytestream=bs2)
    score = dist_to_percent(gis.normalized_distance(img1_sig, img2_sig))

    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'compare',
        'result': [{ 'score': score }]
    })

@app.route('/count', methods=['GET', 'POST'])
def count_handler():
    count = count_images()
    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'count',
        'result': [count]
    })

@app.route('/list', methods=['GET', 'POST'])
def list_handler():
    if request.method == 'GET':
        offset = max(int(request.args.get('offset', 0)), 0)
        limit = max(int(request.args.get('limit', 20)), 0)
    else:
        offset = max(int(request.form.get('offset', 0)), 0)
        limit = max(int(request.form.get('limit', 20)), 0)
    paths = paths_at_location(offset, limit)

    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'list',
        'result': paths
    })

@app.route('/ping', methods=['GET', 'POST'])
def ping_handler():
    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'ping',
        'result': []
    })

# =============================================================================
# Error Handling

@app.errorhandler(400)
def bad_request(e):
    return json.dumps({
        'status': 'fail',
        'error': ['bad request'],
        'method': '',
        'result': []
    }), 400

@app.errorhandler(404)
def page_not_found(e):
    return json.dumps({
        'status': 'fail',
        'error': ['not found'],
        'method': '',
        'result': []
    }), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return json.dumps({
        'status': 'fail',
        'error': ['method not allowed'],
        'method': '',
        'result': []
    }), 405

@app.errorhandler(500)
def server_error(e):
    return json.dumps({
        'status': 'fail',
        'error': [str(e)],
        'method': '',
        'result': []
    }), 500
