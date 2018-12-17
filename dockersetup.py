#!/usr/bin/env python3

# Layer index Docker setup script
#
# Copyright (C) 2018 Intel Corporation
# Author: Amber Elliot <amber.n.elliot@intel.com>
#
# Licensed under the MIT license, see COPYING.MIT for details

# This script will make a cluster of 5 containers:
#
#  - layersapp: the application
#  - layersdb: the database
#  - layersweb: NGINX web server (as a proxy and for serving static content)
#  - layerscelery: Celery (for running background jobs)
#  - layersrabbit: RabbitMQ (required by Celery)
#
# It will build and run these containers and set up the database.

import sys
import os
import argparse
import re
import subprocess
import time
import random
import shutil

def get_args():
    parser = argparse.ArgumentParser(description='Script sets up the Clear Dissector tool with Docker Containers.')

    parser.add_argument('-o', '--hostname', type=str, help='Hostname of your machine. Defaults to localhost if not set.', required=False, default = "localhost")
    parser.add_argument('-p', '--http-proxy', type=str, help='http proxy in the format http://<myproxy:port>', required=False)
    parser.add_argument('-s', '--https-proxy', type=str, help='https proxy in the format http://<myproxy:port>', required=False)
    parser.add_argument('-d', '--databasefile', type=str, help='Location of your database file to import. Must be a .sql file.', required=False)
    parser.add_argument('-m', '--portmapping', type=str, help='Port mapping in the format HOST:CONTAINER. Default is %(default)s', required=False, default='8080:80,8081:443')
    parser.add_argument('--no-https', action="store_true", default=False, help='Disable HTTPS (HTTP only) for web server')
    parser.add_argument('--cert', type=str, help='Existing SSL certificate to use for HTTPS web serving', required=False)
    parser.add_argument('--cert-key', type=str, help='Existing SSL certificate key to use for HTTPS web serving', required=False)

    args = parser.parse_args()

    port = proxymod = ""
    try:
        if args.http_proxy:
            split = args.http_proxy.split(":")
            port = split[2]
            proxymod = split[1].replace("/", "")
    except IndexError:
        raise argparse.ArgumentTypeError("http_proxy must be in format http://<myproxy:port>")

    for entry in args.portmapping.split(','):
        if len(entry.split(":")) != 2:
            raise argparse.ArgumentTypeError("Port mapping must in the format HOST:CONTAINER. Ex: 8080:80. Multiple mappings should be separated by commas.")

    if args.no_https:
        if args.cert or args.cert_key:
            raise argparse.ArgumentTypeError("--no-https and --cert/--cert-key options are mutually exclusive")
    if args.cert and not os.path.exists(args.cert):
        raise argparse.ArgumentTypeError("Specified certificate file %s does not exist" % args.cert)
    if args.cert_key and not os.path.exists(args.cert_key):
        raise argparse.ArgumentTypeError("Specified certificate key file %s does not exist" % args.cert_key)
    if args.cert_key and not args.cert:
        raise argparse.ArgumentTypeError("Certificate key file specified but not certificate")
    cert_key = args.cert_key
    if args.cert and not cert_key:
        cert_key = os.path.splitext(args.cert)[0] + '.key'
        if not os.path.exists(cert_key):
            raise argparse.ArgumentTypeError("Could not find certificate key, please use --cert-key to specify it")

    return args.hostname, args.http_proxy, args.https_proxy, args.databasefile, port, proxymod, args.portmapping, args.no_https, args.cert, cert_key

# Edit http_proxy and https_proxy in Dockerfile
def edit_dockerfile(http_proxy, https_proxy):
    filedata= readfile("Dockerfile")
    newlines = []
    lines = filedata.splitlines()
    for line in lines:
        if "ENV http_proxy" in line and http_proxy:
            newlines.append("ENV http_proxy " + http_proxy + "\n")
        elif "ENV https_proxy" in line and https_proxy:
            newlines.append("ENV https_proxy " + https_proxy + "\n")
        else:
            newlines.append(line + "\n")

    writefile("Dockerfile", ''.join(newlines))


# If using a proxy, add proxy values to git-proxy and uncomment proxy script in .gitconfig
def edit_gitproxy(proxymod, port):
    filedata= readfile("docker/git-proxy")
    newlines = []
    lines = filedata.splitlines()
    for line in lines:
        if "PROXY=" in line:
            newlines.append("PROXY=" + proxymod + "\n")
        elif "PORT=" in line:
            newlines.append("PORT=" + port + "\n")
        else:
            newlines.append(line + "\n")
    writefile("docker/git-proxy", ''.join(newlines))
    filedata = readfile("docker/.gitconfig")
    newdata = filedata.replace("#gitproxy", "gitproxy")
    writefile("docker/.gitconfig", newdata)


# Add hostname, secret key, db info, and email host in docker-compose.yml
def edit_dockercompose(hostname, dbpassword, secretkey, portmapping):
    filedata= readfile("docker-compose.yml")
    in_layersweb = False
    in_layersweb_ports = False
    in_layersweb_ports_format = None
    newlines = []
    lines = filedata.splitlines()
    for line in lines:
        if in_layersweb_ports:
            format = line[0:line.find("-")].replace("#", "")
            if in_layersweb_ports_format:
                if format != in_layersweb_ports_format:
                    in_layersweb_ports = False
                    in_layersweb = False
                else:
                    continue
            else:
                in_layersweb_ports_format = format
                for portmap in portmapping.split(','):
                    newlines.append(format + '- "' + portmap + '"' + "\n")
                continue
        if "layersweb:" in line:
            in_layersweb = True
            newlines.append(line + "\n")
        elif "hostname:" in line:
            format = line[0:line.find("hostname")].replace("#", "")
            newlines.append(format +"hostname: " + hostname + "\n")
        elif '- "SECRET_KEY' in line:
            format = line[0:line.find('- "SECRET_KEY')].replace("#", "")
            newlines.append(format + '- "SECRET_KEY=' + secretkey + '"\n')
        elif '- "DATABASE_PASSWORD' in line:
            format = line[0:line.find('- "DATABASE_PASSWORD')].replace("#", "")
            newlines.append(format + '- "DATABASE_PASSWORD=' + dbpassword + '"\n')
        elif '- "MYSQL_ROOT_PASSWORD' in line:
            format = line[0:line.find('- "MYSQL_ROOT_PASSWORD')].replace("#", "")
            newlines.append(format + '- "MYSQL_ROOT_PASSWORD=' + dbpassword + '"\n')
        elif "ports:" in line:
            if in_layersweb:
                in_layersweb_ports = True
            newlines.append(line + "\n")
        else:
            newlines.append(line + "\n")
    writefile("docker-compose.yml", ''.join(newlines))


def edit_nginx_ssl_conf(hostname, https_port, certdir, certfile, keyfile):
    filedata = readfile('docker/nginx-ssl.conf')
    newlines = []
    lines = filedata.splitlines()
    for line in lines:
        if 'ssl_certificate ' in line:
            format = line[0:line.find('ssl_certificate')]
            newlines.append(format + 'ssl_certificate ' + os.path.join(certdir, certfile) + ';\n')
        elif 'ssl_certificate_key ' in line:
            format = line[0:line.find('ssl_certificate_key')]
            newlines.append(format + 'ssl_certificate_key ' + os.path.join(certdir, keyfile) + ';\n')
            # Add a line for the dhparam file
            newlines.append(format + 'ssl_dhparam ' + os.path.join(certdir, 'dhparam.pem') + ';\n')
        elif 'https://layers.openembedded.org' in line:
            line = line.replace('https://layers.openembedded.org', 'https://%s:%s' % (hostname, https_port))
            newlines.append(line + "\n")
        else:
            line = line.replace('layers.openembedded.org', hostname)
            newlines.append(line + "\n")

    # Write to a different file so we can still replace the hostname next time
    writefile("docker/nginx-ssl-edited.conf", ''.join(newlines))


def edit_dockerfile_web(hostname, no_https):
    filedata = readfile('Dockerfile.web')
    newlines = []
    lines = filedata.splitlines()
    for line in lines:
        if line.startswith('COPY ') and line.endswith('/etc/nginx/nginx.conf'):
            if no_https:
                srcfile = 'docker/nginx.conf'
            else:
                srcfile = 'docker/nginx-ssl-edited.conf'
            line = 'COPY %s /etc/nginx/nginx.conf' % srcfile
        newlines.append(line + "\n")
    writefile("Dockerfile.web", ''.join(newlines))


def generatepasswords(passwordlength):
    return ''.join([random.SystemRandom().choice('abcdefghijklmnopqrstuvwxyz0123456789!@#%^&*-_=+') for i in range(passwordlength)])

def readfile(filename):
    f = open(filename,'r')
    filedata = f.read()
    f.close()
    return filedata

def writefile(filename, data):
    f = open(filename,'w')
    f.write(data)
    f.close()


# Generate secret key and database password
secretkey = generatepasswords(50)
dbpassword = generatepasswords(10)

## Get user arguments and modify config files
hostname, http_proxy, https_proxy, dbfile, port, proxymod, portmapping, no_https, cert, cert_key = get_args()

https_port = None
http_port = None
for portmap in portmapping.split(','):
    outport, inport = portmap.split(':', 1)
    if inport == '443':
        https_port = outport
    elif inport == '80':
        http_port = outport
if (not https_port) and (not no_https):
    print("No HTTPS port mapping (to port 443 inside the container) was specified and --no-https was not specified")
    sys.exit(1)
if not (http_port or https_port):
    print("Port mapping must include a mapping to port 80 or 443 inside the container (or both)")
    sys.exit(1)

if http_proxy:
    edit_gitproxy(proxymod, port)
if http_proxy or https_proxy:
    edit_dockerfile(http_proxy, https_proxy)

edit_dockercompose(hostname, dbpassword, secretkey, portmapping)

edit_dockerfile_web(hostname, no_https)

if not no_https:
    local_cert_dir = os.path.abspath('docker/certs')
    if cert:
        if os.path.abspath(os.path.dirname(cert)) != local_cert_dir:
            shutil.copy(cert, local_cert_dir)
        certfile = os.path.basename(cert)
        if os.path.abspath(os.path.dirname(cert_key)) != local_cert_dir:
            shutil.copy(cert_key, local_cert_dir)
        keyfile = os.path.basename(cert_key)
    else:
        print('')
        print('Generating self-signed SSL certificate. Please specify your hostname (%s) when prompted for the Common Name.' % hostname)
        certfile = 'setup-selfsigned.crt'
        keyfile = 'setup-selfsigned.key'
        return_code = subprocess.call('openssl req -x509 -nodes -days 365 -newkey rsa:2048 -keyout %s -out %s' % (os.path.join(local_cert_dir, keyfile), os.path.join(local_cert_dir, certfile)), shell=True)
        if return_code != 0:
            print("Self-signed certificate generation failed")
            sys.exit(1)
    return_code = subprocess.call('openssl dhparam -out %s 2048' % os.path.join(local_cert_dir, 'dhparam.pem'), shell=True)
    if return_code != 0:
        print("DH group generation failed")
        sys.exit(1)

    edit_nginx_ssl_conf(hostname, https_port, '/opt/cert', certfile, keyfile)

## Start up containers
return_code = subprocess.call("docker-compose up -d", shell=True)
if return_code != 0:
    print("docker-compose up failed")
    sys.exit(1)

# Apply any pending layerindex migrations / initialize the database. Database might not be ready yet; have to wait then poll.
time.sleep(8)
while True:
    time.sleep(2)
    return_code = subprocess.call("docker-compose run --rm layersapp /opt/migrate.sh", shell=True)
    if return_code == 0:
        break
    else:
        print("Database server may not be ready; will try again.")

# Import the user's supplied data
if dbfile:
    return_code = subprocess.call("docker exec -i layersdb mysql -uroot -p" + dbpassword + " layersdb " + " < " + dbfile, shell=True)
    if return_code != 0:
        print("Database import failed")
        sys.exit(1)

## For a fresh database, create an admin account
print("Creating database superuser. Input user name, email, and password when prompted.")
return_code = subprocess.call("docker-compose run --rm layersapp /opt/layerindex/manage.py createsuperuser", shell=True)
if return_code != 0:
    print("Creating superuser failed")
    sys.exit(1)

## Set the volume permissions using debian:stretch since we recently fetched it
return_code = subprocess.call("docker run --rm -v layerindexweb_layersmeta:/opt/workdir debian:stretch chown 500 /opt/workdir && \
         docker run --rm -v layerindexweb_layersstatic:/usr/share/nginx/html debian:stretch chown 500 /usr/share/nginx/html && \
         docker run --rm -v layerindexweb_patchvolume:/opt/imagecompare-patches debian:stretch chown 500 /opt/imagecompare-patches", shell=True)
if return_code != 0:
    print("Setting volume permissions failed")
    sys.exit(1)

## Generate static assets. Run this command again to regenerate at any time (when static assets in the code are updated)
return_code = subprocess.call("docker-compose run --rm -e STATIC_ROOT=/usr/share/nginx/html -v layerindexweb_layersstatic:/usr/share/nginx/html layersapp /opt/layerindex/manage.py collectstatic --noinput", shell = True)
if return_code != 0:
    print("Collecting static files failed")
    sys.exit(1)

print("")
if https_port and not no_https:
    protocol = 'https'
    port = https_port
else:
    protocol = 'http'
    port = http_port
print("The application should now be accessible at %s://%s:%s" % (protocol, hostname, port))