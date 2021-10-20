#!/bin/bash -l

set -e

echo "index url : $1"
echo "build_seq_id : $2"
echo "target folder : $3"

time=$(date)
full_build_seq=''$(date -u +"%Y%m%d%H%M")


cd $3
rm -rf dist/

echo "PWD: $PWD"

ls -sailh

echo "PWD: $PWD"

export version=$(cat version.txt).$full_build_seq
/opt/python/cp37-cp37m/bin/python3.7 setup.py bdist_wheel sdist

if [[ $1 = \http* ]]; then
/opt/python/cp37-cp37m/bin/python3.7 -m twine upload --repository-url=$1 -u $4 -p $5 dist/*
fi

if [[ $1 = \s3* ]]; then
export AWS_ACCESS_KEY_ID=$4
export AWS_SECRET_ACCESS_KEY=$5
export AWS_DEFAULT_REGION=$6

echo "/opt/python/cp37-cp37m/bin/python3.7 -m awscli s3 cp dist/ $1/$2 --recursive"
/opt/python/cp37-cp37m/bin/python3.7 -m awscli s3 cp dist/ $1/$2 --recursive
fi

echo "::set-output name=time::$time"
echo "::set-output name=full_build_id::$version"
