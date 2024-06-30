#!/usr/bin/env python3

from pymongo import MongoClient

conn = MongoClient("localhost")

db = conn["montytest_new"]

db.drop_collection("value_training_data")

db.create_collection("value_training_data", capped=False)
