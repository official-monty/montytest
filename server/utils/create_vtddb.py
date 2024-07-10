#!/usr/bin/env python3

from pymongo import MongoClient

conn = MongoClient("localhost")

db = conn["montytest_new"]

db.drop_collection("vtd")

db.create_collection("vtd", capped=False)
