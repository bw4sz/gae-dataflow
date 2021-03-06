# Copyright 2017 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Define and launch a Dataflow pipeline to analyze recent tweets stored
in the Datastore.
"""

from __future__ import absolute_import

import datetime
import json
import logging
import re

import apache_beam as beam
from apache_beam import combiners
from apache_beam.io.gcp.bigquery import parse_table_schema_from_json
from apache_beam.io.gcp.datastore.v1.datastoreio import ReadFromDatastore
from apache_beam.pvalue import AsDict
from apache_beam.pvalue import AsSingleton

from google.cloud.proto.datastore.v1 import query_pb2
from googledatastore import helper as datastore_helper, PropertyFilter


logging.basicConfig(level=logging.INFO)


class WordExtractingDoFn(beam.DoFn):
  """Parse each tweet text into words, removing some 'stopwords'."""

  def process(self, element):
    content_value = element.properties.get('text', None)
    text_line = ''
    if content_value:
      text_line = content_value.string_value

    words = set([x.lower() for x in re.findall(r'[A-Za-z\']+', text_line)])
    stopwords = [
        'a', 'amp', 'an', 'and', 'are', 'as', 'at', 'be', 'been',
        'but', 'by', 'co', 'do', 'for', 'has', 'have', 'he', 'her', 'his',
        'https', 'if', 'in', 'is', 'it', 'me', 'my', 'no', 'not', 'of', 'on',
        'or', 'rt', 's', 'she', 'so', 't', 'than', 'that', 'the', 'they',
        'this', 'to', 'us', 'was', 'we', 'what', 'with', 'you', 'your'
        'who', 'when', 'via']
    # temp
    stopwords += ['lead', 'scoopit']
    stopwords += list(map(chr, range(97, 123)))
    return list(words - set(stopwords))


class CoOccurExtractingDoFn(beam.DoFn):
  """Parse each tweet text into words, and after removing some 'stopwords',
  emit the bigrams.
  """

  def process(self, element):
    content_value = element.properties.get('text', None)
    text_line = ''
    if content_value:
      text_line = content_value.string_value

    words = set([x.lower() for x in re.findall(r'[A-Za-z\']+', text_line)])
    stopwords = [
        'a', 'amp', 'an', 'and', 'are', 'as', 'at', 'be', 'been',
        'but', 'by', 'co', 'do', 'for', 'has', 'have', 'he', 'her', 'his',
        'https', 'if', 'in', 'is', 'it', 'me', 'my', 'no', 'not', 'of', 'on',
        'or', 'rt', 's', 'she', 'so', 't', 'than', 'that', 'the', 'they',
        'this', 'to', 'us', 'was', 'we', 'what', 'with', 'you', 'your',
        'who', 'when', 'via']
    # temp
    stopwords += ['lead', 'scoopit']
    stopwords += list(map(chr, range(97, 123)))
    pruned_words = list(words - set(stopwords))
    pruned_words.sort()
    import itertools
    return list(itertools.combinations(pruned_words, 2))


class URLExtractingDoFn(beam.DoFn):
  """Extract the urls from each tweet."""

  def process(self, element):
    url_content = element.properties.get('urls', None)
    if url_content:
      urls = url_content.array_value.values
      links = []
      for u in urls:
        links.append(u.string_value.lower())
      return links


def make_query(kind):
  """Creates a Cloud Datastore query to retrieve all entities with a
  'created_at' date > N days ago.
  """
  days = 4
  now = datetime.datetime.now()
  earlier = now - datetime.timedelta(days=days)

  query = query_pb2.Query()
  query.kind.add().name = kind

  datastore_helper.set_property_filter(query.filter, 'created_at',
                                       PropertyFilter.GREATER_THAN,
                                       earlier)

  return query


def process_datastore_tweets(project, dataset, pipeline_options):
  """Creates a pipeline that reads tweets from Cloud Datastore from the last
  N days. The pipeline finds the top most-used words, the top most-tweeted
  URLs, ranks word co-occurrences by an 'interestingness' metric (similar to
  on tf* idf).
  """
  ts = str(datetime.datetime.utcnow())
  p = beam.Pipeline(options=pipeline_options)
  # Create a query to read entities from datastore.
  query = make_query('Tweet')

  # Read entities from Cloud Datastore into a PCollection.
  lines = (p
      | 'read from datastore' >> ReadFromDatastore(project, query, None))

  global_count = AsSingleton(
      lines
      | 'global count' >> beam.combiners.Count.Globally())

  # Count the occurrences of each word.
  percents = (lines
      | 'split' >> (beam.ParDo(WordExtractingDoFn())
                    .with_output_types(unicode))
      | 'pair_with_one' >> beam.Map(lambda x: (x, 1))
      | 'group' >> beam.GroupByKey()
      | 'count' >> beam.Map(lambda (word, ones): (word, sum(ones)))
      | 'in tweets percent' >> beam.Map(
          lambda (word, wsum), gc: (word, float(wsum) / gc), global_count))
  top_percents = (percents
      | 'top 500' >> combiners.Top.Of(500, lambda x, y: x[1] < y[1])
      )
  # Count the occurrences of each expanded url in the tweets
  url_counts = (lines
      | 'geturls' >> (beam.ParDo(URLExtractingDoFn())
                    .with_output_types(unicode))
      | 'urls_pair_with_one' >> beam.Map(lambda x: (x, 1))
      | 'urls_group' >> beam.GroupByKey()
      | 'urls_count' >> beam.Map(lambda (word, ones): (word, sum(ones)))
      | 'urls top 300' >> combiners.Top.Of(300, lambda x, y: x[1] < y[1])
      )

  # Define some inline helper functions.

  def join_cinfo(cooccur, percents):
    """Calculate a co-occurence ranking."""
    import math

    word1 = cooccur[0][0]
    word2 = cooccur[0][1]
    try:
      word1_percent = percents[word1]
      weight1 = 1 / word1_percent
      word2_percent = percents[word2]
      weight2 = 1 / word2_percent
      return (cooccur[0], cooccur[1], cooccur[1] *
              math.log(min(weight1, weight2)))
    except:
      return 0

  def generate_cooccur_schema():
    """BigQuery schema for the word co-occurrence table."""
    json_str = json.dumps({'fields': [
          {'name': 'w1', 'type': 'STRING', 'mode': 'NULLABLE'},
          {'name': 'w2', 'type': 'STRING', 'mode': 'NULLABLE'},
          {'name': 'count', 'type': 'INTEGER', 'mode': 'NULLABLE'},
          {'name': 'log_weight', 'type': 'FLOAT', 'mode': 'NULLABLE'},
          {'name': 'ts', 'type': 'TIMESTAMP', 'mode': 'NULLABLE'}]})
    return parse_table_schema_from_json(json_str)

  def generate_url_schema():
    """BigQuery schema for the urls count table."""
    json_str = json.dumps({'fields': [
          {'name': 'url', 'type': 'STRING', 'mode': 'NULLABLE'},
          {'name': 'count', 'type': 'INTEGER', 'mode': 'NULLABLE'},
          {'name': 'ts', 'type': 'TIMESTAMP', 'mode': 'NULLABLE'}]})
    return parse_table_schema_from_json(json_str)

  def generate_wc_schema():
    """BigQuery schema for the word count table."""
    json_str = json.dumps({'fields': [
          {'name': 'word', 'type': 'STRING', 'mode': 'NULLABLE'},
          {'name': 'percent', 'type': 'FLOAT', 'mode': 'NULLABLE'},
          {'name': 'ts', 'type': 'TIMESTAMP', 'mode': 'NULLABLE'}]})
    return parse_table_schema_from_json(json_str)

  # Now build the rest of the pipeline.
  # Calculate the word co-occurence scores.
  cooccur_rankings = (lines
      | 'getcooccur' >> (beam.ParDo(CoOccurExtractingDoFn()))
      | 'co_pair_with_one' >> beam.Map(lambda x: (x, 1))
      | 'co_group' >> beam.GroupByKey()
      | 'co_count' >> beam.Map(lambda (wordts, ones): (wordts, sum(ones)))
      | 'weights' >> beam.Map(join_cinfo, AsDict(percents))
      | 'co top 300' >> combiners.Top.Of(300, lambda x, y: x[2] < y[2])
      )

  # Format the counts into a PCollection of strings.
  wc_records = top_percents | 'format' >> beam.FlatMap(
      lambda x: [{'word': xx[0], 'percent': xx[1], 'ts': ts} for xx in x])

  url_records = url_counts | 'urls_format' >> beam.FlatMap(
      lambda x: [{'url': xx[0], 'count': xx[1], 'ts': ts} for xx in x])

  co_records = cooccur_rankings | 'co_format' >> beam.FlatMap(
      lambda x: [{'w1': xx[0][0], 'w2': xx[0][1], 'count': xx[1],
      'log_weight': xx[2], 'ts': ts} for xx in x])

  # Write the results to three BigQuery tables.
  wc_records | 'wc_write_bq' >> beam.io.Write(
      beam.io.BigQuerySink(
          '%s:%s.word_counts' % (project, dataset),
          schema=generate_wc_schema(),
          create_disposition=beam.io.BigQueryDisposition.CREATE_IF_NEEDED,
          write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND))

  url_records | 'urls_write_bq' >> beam.io.Write(
      beam.io.BigQuerySink(
          '%s:%s.urls' % (project, dataset),
          schema=generate_url_schema(),
          create_disposition=beam.io.BigQueryDisposition.CREATE_IF_NEEDED,
          write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND))

  co_records | 'co_write_bq' >> beam.io.Write(
      beam.io.BigQuerySink(
          '%s:%s.word_cooccur' % (project, dataset),
          schema=generate_cooccur_schema(),
          create_disposition=beam.io.BigQueryDisposition.CREATE_IF_NEEDED,
          write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND))

  # Actually run the pipeline.
  return p.run()


