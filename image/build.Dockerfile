FROM quay.io/pypa/manylinux_2_24_x86_64:latest
RUN /opt/python/cp37-cp37m/bin/pip install --upgrade twine pip
COPY build.sh /project/build.sh
RUN chmod +x /project/build.sh
RUN /opt/python/cp37-cp37m/bin/pip install awscli
ENTRYPOINT ["/project/build.sh"]
