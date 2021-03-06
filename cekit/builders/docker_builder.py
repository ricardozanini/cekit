import logging
import os
import re
import sys
import traceback

import yaml

from cekit.builder import Builder
from cekit.errors import CekitError

LOGGER = logging.getLogger('cekit')

# Ignore any failure on non-core modules, we will catch it later
# and suggest a solution
try:
    # Squash library
    from docker_squash.squash import Squash
except ImportError:
    pass

try:
    # Docker Python library, the old one
    from docker.api.client import APIClient as APIClientClass
except ImportError:
    pass

try:
    # The requests library is an indirect dependency, we need to put it here
    # so that the dependency mechanism can kick in and require the docker library
    # first which will pull requests
    import requests
except ImportError:
    pass

try:
    # Docker Python library, the new one
    from docker.client import Client as APIClientClass  # pylint: disable=ungrouped-imports
except ImportError:
    pass

ANSI_ESCAPE = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')


class DockerBuilder(Builder):
    """This class wraps docker build command to build and image"""

    def __init__(self, common_params, params):
        super(DockerBuilder, self).__init__('docker', common_params, params)

        # Default Docker daemon connection timeout 10 minutes
        # It needs to be high enough to allow Docker daemon to export the
        # image for squashing.
        try:
            self.timeout = int(os.getenv('DOCKER_TIMEOUT', '600'))
        except ValueError:
            raise CekitError("Provided timeout value: %s cannot be parsed as integer, exiting." %
                             os.getenv('DOCKER_TIMEOUT'))

        if self.timeout <= 0:
            raise CekitError(
                "Provided timeout value needs to be greater than zero, currently: %s, exiting." % self.timeout)

    @staticmethod
    def dependencies():
        deps = {}

        deps['python-docker'] = {
            'library': 'docker',
            'package': 'python-docker-py',
            'fedora': {
                'package': 'python3-docker'}
        }

        deps['docker-squash'] = {
            'library': 'docker_squash',
            'fedora': {
                'package': 'python3-docker-squash'
            }
        }

        return deps

    def _build_with_docker(self, docker_client):
        docker_args = {}
        docker_args['path'] = os.path.join(self.target, 'image')
        docker_args['pull'] = self.params.pull
        docker_args['rm'] = True

        build_log = [""]
        docker_layer_ids = []

        try:
            out = docker_client.build(**docker_args)
            for line in out:
                if b'stream' in line:
                    line = yaml.safe_load(line)['stream']
                elif b'status' in line:
                    line = yaml.safe_load(line)['status']
                elif b'errorDetail' in line:
                    line = yaml.safe_load(line)['errorDetail']['message']
                    raise CekitError("Image build failed: '%s'" % line)

                if line != build_log[-1]:
                    # this prevents poluting cekit log with dowloading/extracting msgs
                    log_msg = ANSI_ESCAPE.sub('', line).strip()
                    for msg in log_msg.split('\n'):
                        LOGGER.info('Docker: %s' % msg)
                    build_log.append(line)

                    layer_id_match = re.search(r'^---> ([\w]{12})$', line.strip())

                    if layer_id_match:
                        docker_layer_ids.append(layer_id_match.group(1))

        except requests.ConnectionError as ex:
            exception_chain = traceback.format_exc()
            LOGGER.debug("Caught ConnectionError attempting to communicate with Docker ", exc_info=1)

            if 'PermissionError' in exception_chain:
                message = "Unable to contact docker daemon. Is it correctly setup?\n" \
                          "See https://developer.fedoraproject.org/tools/docker/docker-installation.html and " \
                          "http://www.projectatomic.io/blog/2015/08/why-we-dont-let-non-root-users-run-docker-in-centos-fedora-or-rhel"
            elif 'FileNotFoundError' in exception_chain:
                message = "Unable to contact docker daemon. Is it started?"
            else:
                message = "Unknown ConnectionError from docker ; is the daemon started and correctly setup?"

            if sys.version_info.major == 3:
                # Work-around for python 2 / 3 code - replicate exception(...) from None
                cekit_exception = CekitError(message, ex)
                cekit_exception.__cause__ = None
                raise cekit_exception
            else:
                raise CekitError(message, ex)

        except Exception as ex:
            msg = "Image build failed, see logs above."
            if len(docker_layer_ids) >= 2:
                LOGGER.error("You can look inside the failed image by running "
                             "'docker run --rm -ti %s bash'" % docker_layer_ids[-1])
            if "To enable Red Hat Subscription Management repositories:" in ' '.join(build_log) and \
                    not os.path.exists(os.path.join(self.target, 'image', 'repos')):
                msg = "Image build failed with a yum error and you don't " \
                      "have any yum repository configured, please check " \
                      "your image/module descriptor for proper repository " \
                      " definitions."
            raise CekitError(msg, ex)

        return docker_layer_ids[-1]

    def _squash(self, docker_client, image_id):
        LOGGER.info("Squashing image %s..." % image_id)

        squash = Squash(docker=docker_client,
                        log=LOGGER,
                        from_layer=self.generator.image['from'],
                        image=image_id,
                        cleanup=True)
        return squash.run()

    def _tag(self, docker_client, image_id, tags):
        for tag in tags:
            if ':' in tag:
                img_repo, img_tag = tag.rsplit(":", 1)
                docker_client.tag(image_id, img_repo, tag=img_tag)
            else:
                docker_client.tag(image_id, tag)

    def run(self):
        tags = self.params.tags

        if not tags:
            tags = self.generator.get_tags()

        LOGGER.info("Building container image using Docker...")
        LOGGER.debug("Building image with tags: '%s'" % "', '".join(tags))

        docker_client = APIClientClass(version="1.22", timeout=self.timeout)

        # Build image
        image_id = self._build_with_docker(docker_client)

        # Squash only if --no-squash is NOT defined
        if not self.params.no_squash:
            image_id = self._squash(docker_client, image_id)

        # Tag the image
        self._tag(docker_client, image_id, tags)

        LOGGER.info("Image built and available under following tags: %s" %
                    ", ".join(tags))
