#!/bin/bash
set -e

awslocal s3 mb s3://submissions-checker
awslocal s3api put-bucket-acl --bucket submissions-checker --acl public-read

echo "S3 bucket 'submissions-checker' created and set to public-read."
