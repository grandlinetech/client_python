from __future__ import unicode_literals

import base64
from contextlib import closing
import os
import socket
import sys
import threading
from wsgiref.simple_server import make_server, WSGIRequestHandler, WSGIServer
from wsgiref.handlers import SimpleHandler

from .openmetrics import exposition as openmetrics
from .registry import REGISTRY
from .utils import floatToGoString
from platform import python_implementation

try:
    from urllib import quote_plus

    from BaseHTTPServer import BaseHTTPRequestHandler
    from SocketServer import ThreadingMixIn
    from urllib2 import (
        build_opener, HTTPError, HTTPHandler, HTTPRedirectHandler, Request,
    )
    from urlparse import parse_qs, urlparse
except ImportError:
    # Python 3
    from http.server import BaseHTTPRequestHandler
    from socketserver import ThreadingMixIn
    from urllib.error import HTTPError
    from urllib.parse import parse_qs, quote_plus, urlparse
    from urllib.request import (
        build_opener, HTTPHandler, HTTPRedirectHandler, Request,
    )

CONTENT_TYPE_LATEST = str('text/plain; version=0.0.4; charset=utf-8')
"""Content type of the latest text format"""
PYTHON27_OR_OLDER = sys.version_info < (3,)
PYTHON26_OR_OLDER = sys.version_info < (2, 7)
PYTHON376_OR_NEWER = sys.version_info > (3, 7, 5)

__version__ = "0.2"

server_version = "WSGIServer/" + __version__
sys_version = python_implementation() + "/" + sys.version.split()[0]
software_version = server_version + ' ' + sys_version


class _PrometheusRedirectHandler(HTTPRedirectHandler):
    """
    Allow additional methods (e.g. PUT) and data forwarding in redirects.

    Use of this class constitute a user's explicit agreement to the
    redirect responses the Prometheus client will receive when using it.
    You should only use this class if you control or otherwise trust the
    redirect behavior involved and are certain it is safe to full transfer
    the original request (method and data) to the redirected URL. For
    example, if you know there is a cosmetic URL redirect in front of a
    local deployment of a Prometheus server, and all redirects are safe,
    this is the class to use to handle redirects in that case.

    The standard HTTPRedirectHandler does not forward request data nor
    does it allow redirected PUT requests (which Prometheus uses for some
    operations, for example `push_to_gateway`) because these cannot
    generically guarantee no violations of HTTP RFC 2616 requirements for
    the user to explicitly confirm redirects that could have unexpected
    side effects (such as rendering a PUT request non-idempotent or
    creating multiple resources not named in the original request).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """
        Apply redirect logic to a request.

        See parent HTTPRedirectHandler.redirect_request for parameter info.

        If the redirect is disallowed, this raises the corresponding HTTP error.
        If the redirect can't be determined, return None to allow other handlers
        to try. If the redirect is allowed, return the new request.

        This method specialized for the case when (a) the user knows that the
        redirect will not cause unacceptable side effects for any request method,
        and (b) the user knows that any request data should be passed through to
        the redirect. If either condition is not met, this should not be used.
        """
        # note that requests being provided by a handler will use get_method to
        # indicate the method, by monkeypatching this, instead of setting the
        # Request object's method attribute.
        m = getattr(req, "method", req.get_method())
        if not (code in (301, 302, 303, 307) and m in ("GET", "HEAD")
                or code in (301, 302, 303) and m in ("POST", "PUT")):
            raise HTTPError(req.full_url, code, msg, headers, fp)
        new_request = Request(
            newurl.replace(' ', '%20'),  # space escaping in new url if needed.
            headers=req.headers,
            origin_req_host=req.origin_req_host,
            unverifiable=True,
            data=req.data,
        )
        if PYTHON27_OR_OLDER:
            # the `method` attribute did not exist for Request in Python 2.7.
            new_request.get_method = lambda: m
        else:
            new_request.method = m
        return new_request


def _bake_output(registry, accept_header, params):
    """Bake output for metrics output."""
    encoder, content_type = choose_encoder(accept_header)
    if 'name[]' in params:
        registry = registry.restricted_registry(params['name[]'])
    output = encoder(registry)
    return str('200 OK'), (str('Content-Type'), content_type), output


def make_wsgi_app(registry=REGISTRY):
    """Create a WSGI app which serves the metrics from a registry."""

    def prometheus_app(environ, start_response):
        # Prepare parameters
        accept_header = environ.get('HTTP_ACCEPT')
        params = parse_qs(environ.get('QUERY_STRING', ''))
        if environ['PATH_INFO'] == '/favicon.ico':
            # Serve empty response for browsers
            status = '200 OK'
            header = ('', '')
            output = b''
        else:
            # Bake output
            status, header, output = _bake_output(registry, accept_header, params)
        # Return output
        start_response(status, [header])
        return [output]

    return prometheus_app


class ServerHandler2(SimpleHandler):
    server_software = software_version

    def log_exception(self, exc_info):
        exc_info = None

    def get_stderr(self):
        f = open(os.devnull, 'w')
        return f

    def close(self):
        try:
            self.request_handler.log_request(
                '', self.bytes_sent
            )
        finally:
            SimpleHandler.close(self)

    def handle_error(self) -> None:
        pass


class _SilentHandler(WSGIRequestHandler):
    """WSGI handler that does not log requests."""

    def log_message(self, format, *args):
        """Log nothing."""

    def get_stderr(self):
        f = open(os.devnull, 'wb')
        return f

    def handle(self):
        """Handle a single HTTP request"""

        self.raw_requestline = self.rfile.readline(65537)
        if len(self.raw_requestline) > 65536:
            self.requestline = ''
            self.request_version = ''
            self.command = ''
            self.send_error(414)
            return

        if not self.parse_request():  # An error code has been sent, just exit
            return

        handler = ServerHandler2(
            self.rfile, self.wfile, self.get_stderr(), self.get_environ(),
            multithread=False,
        )
        handler.request_handler = self  # backpointer for logging
        handler.run(self.server.get_app())


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """Thread per request HTTP server."""
    # Make worker threads "fire and forget". Beginning with Python 3.7 this
    # prevents a memory leak because ``ThreadingMixIn`` starts to gather all
    # non-daemon threads in a list in order to join on them at server close.
    daemon_threads = True


def start_wsgi_server(port, addr='', registry=REGISTRY):
    """Starts a WSGI server for prometheus metrics as a daemon thread."""
    app = make_wsgi_app(registry)
    httpd = make_server(addr, port, app, ThreadingWSGIServer, handler_class=_SilentHandler)
    t = threading.Thread(target=httpd.serve_forever)
    t.daemon = True
    t.start()


start_http_server = start_wsgi_server


def generate_latest(registry=REGISTRY):
    """Returns the metrics from the registry in latest text format as a string."""

    def sample_line(line):
        if line.labels:
            labelstr = '{{{0}}}'.format(','.join(
                ['{0}="{1}"'.format(
                    k, v.replace('\\', r'\\').replace('\n', r'\n').replace('"', r'\"'))
                    for k, v in sorted(line.labels.items())]))
        else:
            labelstr = ''
        timestamp = ''
        if line.timestamp is not None:
            # Convert to milliseconds.
            timestamp = ' {0:d}'.format(int(float(line.timestamp) * 1000))
        return '{0}{1} {2}{3}\n'.format(
            line.name, labelstr, floatToGoString(line.value), timestamp)

    output = []
    for metric in registry.collect():
        try:
            mname = metric.name
            mtype = metric.type
            # Munging from OpenMetrics into Prometheus format.
            if mtype == 'counter':
                mname = mname + '_total'
            elif mtype == 'info':
                mname = mname + '_info'
                mtype = 'gauge'
            elif mtype == 'stateset':
                mtype = 'gauge'
            elif mtype == 'gaugehistogram':
                # A gauge histogram is really a gauge,
                # but this captures the structure better.
                mtype = 'histogram'
            elif mtype == 'unknown':
                mtype = 'untyped'

            output.append('# HELP {0} {1}\n'.format(
                mname, metric.documentation.replace('\\', r'\\').replace('\n', r'\n')))
            output.append('# TYPE {0} {1}\n'.format(mname, mtype))

            om_samples = {}
            for s in metric.samples:
                for suffix in ['_created', '_gsum', '_gcount']:
                    if s.name == metric.name + suffix:
                        # OpenMetrics specific sample, put in a gauge at the end.
                        om_samples.setdefault(suffix, []).append(sample_line(s))
                        break
                else:
                    output.append(sample_line(s))
        except Exception as exception:
            exception.args = (exception.args or ('',)) + (metric,)
            raise

        for suffix, lines in sorted(om_samples.items()):
            output.append('# HELP {0}{1} {2}\n'.format(metric.name, suffix,
                                                       metric.documentation.replace('\\', r'\\').replace('\n', r'\n')))
            output.append('# TYPE {0}{1} gauge\n'.format(metric.name, suffix))
            output.extend(lines)
    return ''.join(output).encode('utf-8')


def choose_encoder(accept_header):
    accept_header = accept_header or ''
    for accepted in accept_header.split(','):
        if accepted.split(';')[0].strip() == 'application/openmetrics-text':
            return (openmetrics.generate_latest,
                    openmetrics.CONTENT_TYPE_LATEST)
    return generate_latest, CONTENT_TYPE_LATEST


class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP handler that gives metrics from ``REGISTRY``."""
    registry = REGISTRY

    def do_GET(self):
        # Prepare parameters
        registry = self.registry
        accept_header = self.headers.get('Accept')
        params = parse_qs(urlparse(self.path).query)
        # Bake output
        status, header, output = _bake_output(registry, accept_header, params)
        # Return output
        self.send_response(int(status.split(' ')[0]))
        self.send_header(*header)
        self.end_headers()
        self.wfile.write(output)

    def log_message(self, format, *args):
        """Log nothing."""

    @classmethod
    def factory(cls, registry):
        """Returns a dynamic MetricsHandler class tied
           to the passed registry.
        """
        # This implementation relies on MetricsHandler.registry
        #  (defined above and defaulted to REGISTRY).

        # As we have unicode_literals, we need to create a str()
        #  object for type().
        cls_name = str(cls.__name__)
        MyMetricsHandler = type(cls_name, (cls, object),
                                {"registry": registry})
        return MyMetricsHandler


def write_to_textfile(path, registry):
    """Write metrics to the given path.

    This is intended for use with the Node exporter textfile collector.
    The path must end in .prom for the textfile collector to process it."""
    tmppath = '%s.%s.%s' % (path, os.getpid(), threading.current_thread().ident)
    with open(tmppath, 'wb') as f:
        f.write(generate_latest(registry))

    # rename(2) is atomic but fails on Windows if the destination file exists
    if os.name == 'nt':
        if sys.version_info <= (3, 3):
            # Unable to guarantee atomic rename on Windows and Python<3.3
            # Remove and rename instead (risks losing the file)
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

            os.rename(tmppath, path)
        else:
            # os.replace is introduced in Python 3.3 but there is some dispute whether
            # it is a truly atomic file operation: https://bugs.python.org/issue8828
            os.replace(tmppath, path)
    else:
        os.rename(tmppath, path)


def _make_handler(url, method, timeout, headers, data, base_handler):
    def handle():
        request = Request(url, data=data)
        request.get_method = lambda: method
        for k, v in headers:
            request.add_header(k, v)
        resp = build_opener(base_handler).open(request, timeout=timeout)
        if resp.code >= 400:
            raise IOError("error talking to pushgateway: {0} {1}".format(
                resp.code, resp.msg))

    return handle


def default_handler(url, method, timeout, headers, data):
    """Default handler that implements HTTP/HTTPS connections.

    Used by the push_to_gateway functions. Can be re-used by other handlers."""

    return _make_handler(url, method, timeout, headers, data, HTTPHandler)


def passthrough_redirect_handler(url, method, timeout, headers, data):
    """
    Handler that automatically trusts redirect responses for all HTTP methods.

    Augments standard HTTPRedirectHandler capability by permitting PUT requests,
    preserving the method upon redirect, and passing through all headers and
    data from the original request. Only use this handler if you control or
    trust the source of redirect responses you encounter when making requests
    via the Prometheus client. This handler will simply repeat the identical
    request, including same method and data, to the new redirect URL."""

    return _make_handler(url, method, timeout, headers, data, _PrometheusRedirectHandler)


def basic_auth_handler(url, method, timeout, headers, data, username=None, password=None):
    """Handler that implements HTTP/HTTPS connections with Basic Auth.

    Sets auth headers using supplied 'username' and 'password', if set.
    Used by the push_to_gateway functions. Can be re-used by other handlers."""

    def handle():
        """Handler that implements HTTP Basic Auth.
        """
        if username is not None and password is not None:
            auth_value = '{0}:{1}'.format(username, password).encode('utf-8')
            auth_token = base64.b64encode(auth_value)
            auth_header = b'Basic ' + auth_token
            headers.append(['Authorization', auth_header])
        default_handler(url, method, timeout, headers, data)()

    return handle


def push_to_gateway(
        gateway, job, registry, grouping_key=None, timeout=30,
        handler=default_handler):
    """Push metrics to the given pushgateway.

    `gateway` the url for your push gateway. Either of the form
              'http://pushgateway.local', or 'pushgateway.local'.
              Scheme defaults to 'http' if none is provided
    `job` is the job label to be attached to all pushed metrics
    `registry` is an instance of CollectorRegistry
    `grouping_key` please see the pushgateway documentation for details.
                   Defaults to None
    `timeout` is how long push will attempt to connect before giving up.
              Defaults to 30s, can be set to None for no timeout.
    `handler` is an optional function which can be provided to perform
              requests to the 'gateway'.
              Defaults to None, in which case an http or https request
              will be carried out by a default handler.
              If not None, the argument must be a function which accepts
              the following arguments:
              url, method, timeout, headers, and content
              May be used to implement additional functionality not
              supported by the built-in default handler (such as SSL
              client certicates, and HTTP authentication mechanisms).
              'url' is the URL for the request, the 'gateway' argument
              described earlier will form the basis of this URL.
              'method' is the HTTP method which should be used when
              carrying out the request.
              'timeout' requests not successfully completed after this
              many seconds should be aborted.  If timeout is None, then
              the handler should not set a timeout.
              'headers' is a list of ("header-name","header-value") tuples
              which must be passed to the pushgateway in the form of HTTP
              request headers.
              The function should raise an exception (e.g. IOError) on
              failure.
              'content' is the data which should be used to form the HTTP
              Message Body.

    This overwrites all metrics with the same job and grouping_key.
    This uses the PUT HTTP method."""
    _use_gateway('PUT', gateway, job, registry, grouping_key, timeout, handler)


def pushadd_to_gateway(
        gateway, job, registry, grouping_key=None, timeout=30,
        handler=default_handler):
    """PushAdd metrics to the given pushgateway.

    `gateway` the url for your push gateway. Either of the form
              'http://pushgateway.local', or 'pushgateway.local'.
              Scheme defaults to 'http' if none is provided
    `job` is the job label to be attached to all pushed metrics
    `registry` is an instance of CollectorRegistry
    `grouping_key` please see the pushgateway documentation for details.
                   Defaults to None
    `timeout` is how long push will attempt to connect before giving up.
              Defaults to 30s, can be set to None for no timeout.
    `handler` is an optional function which can be provided to perform
              requests to the 'gateway'.
              Defaults to None, in which case an http or https request
              will be carried out by a default handler.
              See the 'prometheus_client.push_to_gateway' documentation
              for implementation requirements.

    This replaces metrics with the same name, job and grouping_key.
    This uses the POST HTTP method."""
    _use_gateway('POST', gateway, job, registry, grouping_key, timeout, handler)


def delete_from_gateway(
        gateway, job, grouping_key=None, timeout=30, handler=default_handler):
    """Delete metrics from the given pushgateway.

    `gateway` the url for your push gateway. Either of the form
              'http://pushgateway.local', or 'pushgateway.local'.
              Scheme defaults to 'http' if none is provided
    `job` is the job label to be attached to all pushed metrics
    `grouping_key` please see the pushgateway documentation for details.
                   Defaults to None
    `timeout` is how long delete will attempt to connect before giving up.
              Defaults to 30s, can be set to None for no timeout.
    `handler` is an optional function which can be provided to perform
              requests to the 'gateway'.
              Defaults to None, in which case an http or https request
              will be carried out by a default handler.
              See the 'prometheus_client.push_to_gateway' documentation
              for implementation requirements.

    This deletes metrics with the given job and grouping_key.
    This uses the DELETE HTTP method."""
    _use_gateway('DELETE', gateway, job, None, grouping_key, timeout, handler)


def _use_gateway(method, gateway, job, registry, grouping_key, timeout, handler):
    gateway_url = urlparse(gateway)
    # See https://bugs.python.org/issue27657 for details on urlparse in py>=3.7.6.
    if not gateway_url.scheme or (
            (PYTHON376_OR_NEWER or PYTHON26_OR_OLDER)
            and gateway_url.scheme not in ['http', 'https']
    ):
        gateway = 'http://{0}'.format(gateway)
    url = '{0}/metrics/{1}/{2}'.format(gateway, *_escape_grouping_key("job", job))

    data = b''
    if method != 'DELETE':
        data = generate_latest(registry)

    if grouping_key is None:
        grouping_key = {}
    url += ''.join(
        '/{0}/{1}'.format(*_escape_grouping_key(str(k), str(v)))
        for k, v in sorted(grouping_key.items()))

    handler(
        url=url, method=method, timeout=timeout,
        headers=[('Content-Type', CONTENT_TYPE_LATEST)], data=data,
    )()


def _escape_grouping_key(k, v):
    if v == "":
        # Per https://github.com/prometheus/pushgateway/pull/346.
        return k + "@base64", "="
    elif '/' in v:
        # Added in Pushgateway 0.9.0.
        return k + "@base64", base64.urlsafe_b64encode(v.encode("utf-8")).decode("utf-8")
    else:
        return k, quote_plus(v)


def instance_ip_grouping_key():
    """Grouping key with instance set to the IP Address of this host."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
        if sys.platform == 'darwin':
            # This check is done this way only on MacOS devices
            # it is done this way because the localhost method does
            # not work.
            # This method was adapted from this StackOverflow answer:
            # https://stackoverflow.com/a/28950776
            s.connect(('10.255.255.255', 1))
        else:
            s.connect(('localhost', 0))

        return {'instance': s.getsockname()[0]}


try:
    # Python >3.5 only
    from .asgi import make_asgi_app  # noqa
except:
    pass
