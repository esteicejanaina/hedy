import sys
if (sys.version_info.major < 3 or sys.version_info.minor < 6):
    print ('Hedy requires Python 3.6 or newer to run. However, your version of Python is', '.'.join ([str (sys.version_info.major), str (sys.version_info.minor), str (sys.version_info.micro)]))
    quit ()

# coding=utf-8
import datetime
import collections
from functools import wraps
import hedy
import json
import jsonbin
import logging
import os
from os import path
import re
import requests
import uuid
import yaml
from flask_commonmark import Commonmark
from werkzeug.urls import url_encode
from config import config
from auth import auth_templates, current_user, requires_login, is_admin, is_teacher
from utils import db_get, db_get_many, db_set, timems, type_check, object_check, db_del
import hashlib

# app.py
from flask import Flask, request, jsonify, render_template, session, abort, g, redirect, make_response
from flask_compress import Compress

# Hedy-specific modules
import courses
import hedyweb

# Define and load all available language data
ALL_LANGUAGES = {
    'en': 'English',
    'nl': 'Nederlands',
    'es': 'Español',
    'fr': 'Français',
    'pt_br': 'Português',
    'de': 'Deutsch',
    'it': 'Italiano'
}

LEVEL_DEFAULTS = collections.defaultdict(courses.NoSuchDefaults)
for lang in ALL_LANGUAGES.keys():
    LEVEL_DEFAULTS[lang] = courses.LevelDefaults(lang)

HEDY_COURSE = collections.defaultdict(courses.NoSuchCourse)
for lang in ALL_LANGUAGES.keys():
    HEDY_COURSE[lang] = courses.Course('hedy', lang, LEVEL_DEFAULTS[lang])

SPACE_EU_COURSE = {'nl': courses.Course('space_eu', 'nl', LEVEL_DEFAULTS['nl']),
                   'en': courses.Course('space_eu', 'en', LEVEL_DEFAULTS['en']),
                   'es': courses.Course('space_eu', 'es', LEVEL_DEFAULTS['es'])
                   }

ONLINE_MASTERS_COURSE = courses.Course('online_masters', 'nl', LEVEL_DEFAULTS['nl'])

TRANSLATIONS = hedyweb.Translations()

# Load main menu (do it once, can be cached)
with open(f'main/menu.json', 'r', encoding='utf8') as f:
    main_menu_json = json.load(f)


logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] %(levelname)-8s: %(message)s')

app = Flask(__name__, static_url_path='')

def hash_user_or_session (string):
    hash = hashlib.md5 (string.encode ('utf-8')).hexdigest ()
    return int (hash, 16)

def redirect_ab (request, session):
    # If the user is logged in, we use their username as identifier, otherwise we use the session id
    user_identifier = current_user(request) ['username'] or str (session_id ())

    # This will send 50% of the requests into redirect.
    redirect_proportion = 50
    redirect_flag = (hash_user_or_session (user_identifier) % 100) < redirect_proportion
    return redirect_flag

# If present, PROXY_TO_TEST_ENV should be the name of the target environment
if os.getenv ('PROXY_TO_TEST_ENV') and not os.getenv ('IS_TEST_ENV'):
    @app.before_request
    def before_request_proxy():
        # If it is an auth route, we do not reverse proxy it to the PROXY_TO_TEST_ENV environment, with the exception of /auth/texts
        # We want to keep all cookie setting in the main environment, not the test one.
        if (re.match ('.*/auth/.*', request.url) and not re.match ('.*/auth/texts', request.url)):
            pass
        # If we enter this block, we will reverse proxy the request to the PROXY_TO_TEST_ENV environment.
        elif (redirect_ab (request, session)):

            print ('DEBUG TEST - REVERSE PROXYING REQUEST', request.method, request.url, session_id ())

            url = request.url.replace (os.getenv ('HEROKU_APP_NAME'), os.getenv ('PROXY_TO_TEST_ENV'))

            request_headers = {}
            for header in request.headers:
                if (header [0].lower () in ['host']):
                    continue
                request_headers [header [0]] = header [1]

            # Send the session_id to the test environment for logging purposes
            request_headers ['x-session_id'] = session_id ()
            r = getattr (requests, request.method.lower ()) (url, headers=request_headers, data=request.data)

            response = make_response (r.content)
            for header in r.headers:
                # With great help from https://medium.com/customorchestrator/simple-reverse-proxy-server-using-flask-936087ce0afb
                # We ignore the set-cookie header
                if (header.lower () in ['content-encoding', 'content-length', 'transfer-encoding', 'connection', 'set-cookie']):
                    continue
                response.headers [header] = r.headers [header]
            return response, r.status_code

if os.getenv ('IS_TEST_ENV'):
    @app.before_request
    def before_request_receive_proxy():
        print ('DEBUG TEST - RECEIVE PROXIED REQUEST', request.method, request.url, session_id ())

# HTTP -> HTTPS redirect
# https://stackoverflow.com/questions/32237379/python-flask-redirect-to-https-from-http/32238093
if os.getenv ('REDIRECT_HTTP_TO_HTTPS'):
    @app.before_request
    def before_request_https():
        if request.url.startswith('http://'):
            url = request.url.replace('http://', 'https://', 1)
            # We use a 302 in case we need to revert the redirect.
            return redirect(url, code=302)

# Unique random key for sessions.
# For settings with multiple workers, an environment variable is required, otherwise cookies will be constantly removed and re-set by different workers.
app.config['SECRET_KEY'] = os.getenv ('SECRET_KEY') or uuid.uuid4().hex

Compress(app)
Commonmark(app)
logger = jsonbin.JsonBinLogger.from_env_vars()

if not os.getenv('HEROKU_RELEASE_CREATED_AT'):
    logging.warning('Cannot determine release; enable Dyno metadata by running "heroku labs:enable runtime-dyno-metadata -a <APP_NAME>"')

@app.route('/parse', methods=['POST'])
def parse():
    body = request.json
    if not body:
        return "body must be an object", 400
    if 'code' not in body:
        return "body.code must be a string", 400
    if 'level' not in body:
        return "body.level must be a string", 400

    code = body ['code']
    level = int(body ['level'])
    # Language should come principally from the request body,
    # but we'll fall back to browser default if it's missing for whatever
    # reason.
    lang = body.get('lang', requested_lang())

    # For debugging
    print(f"got code {code}")

    response = {}
    username = current_user(request) ['username'] or None

    # Check if user sent code
    if not code:
        response["Error"] = "no code found, please send code."
    # is so, parse
    else:
        try:
            hedy_errors = TRANSLATIONS.get_translations(lang, 'HedyErrorMessages')
            result = hedy.transpile(code, level)
            response["Code"] = "# coding=utf8\nimport random\n" + result
        except hedy.HedyException as E:
            # some 'errors' can be fixed, for these we throw an exception, but also
            # return fixed code, so it can be ran
            if E.args[0] == "Invalid Space":
                error_template = hedy_errors[E.error_code]
                response["Code"] = "# coding=utf8\n" + E.arguments['fixed_code']
                response["Warning"] = error_template.format(**E.arguments)
            elif E.args[0] == "Parse":
                error_template = hedy_errors[E.error_code]
                # Localize the names of characters
                if 'character_found' in E.arguments:
                    E.arguments['character_found'] = hedy_errors[E.arguments['character_found']]
                response["Error"] = error_template.format(**E.arguments)
            elif E.args[0] == "Unquoted Text":
                error_template = hedy_errors[E.error_code]
                response["Error"] = error_template.format(**E.arguments)
            else:
                error_template = hedy_errors[E.error_code]
                response["Error"] = error_template.format(**E.arguments)
        except Exception as E:
            print(f"error transpiling {code}")
            response["Error"] = str(E)

    logger.log ({
        'session': session_id(),
        'date': str(datetime.datetime.now()),
        'level': level,
        'lang': lang,
        'code': code,
        'server_error': response.get('Error'),
        'version': version(),
        'username': username,
        'is_test': 1 if os.getenv ('IS_TEST_ENV') else None
    })

    return jsonify(response)

@app.route('/report_error', methods=['POST'])
def report_error():
    post_body = request.json

    logger.log ({
        'session': session_id(),
        'date': str(datetime.datetime.now()),
        'level': post_body.get('level'),
        'code': post_body.get('code'),
        'client_error': post_body.get('client_error'),
        'version': version(),
        'username': current_user(request) ['username'] or None,
        'is_test': 1 if os.getenv ('IS_TEST_ENV') else None
    })

    return 'logged'

@app.route('/version', methods=['GET'])
def version_page():
    """
    Generate a page with some diagnostic information and a useful GitHub URL on upcoming changes.

    This is an admin-only page, it does not need to be linked.
    (Also does not have any sensitive information so it's fine to be unauthenticated).
    """
    app_name = os.getenv('HEROKU_APP_NAME')

    vrz = os.getenv('HEROKU_RELEASE_CREATED_AT')
    the_date = datetime.date.fromisoformat(vrz[:10]) if vrz else datetime.date.today()

    commit = os.getenv('HEROKU_SLUG_COMMIT', '????')[0:6]

    return render_template('version-page.html',
        app_name=app_name,
        heroku_release_time=the_date,
        commit=commit)


def programs_page (request):
    username = current_user(request) ['username']
    if not username:
        return "unauthorized", 403

    lang = requested_lang()
    query_lang = request.args.get('lang') or ''
    if query_lang:
        query_lang = '?lang=' + query_lang

    from_user = request.args.get('user') or None
    if from_user and not is_admin (request):
        return "unauthorized", 403

    texts=TRANSLATIONS.data [lang] ['Programs']

    result = db_get_many ('programs', {'username': from_user or username}, True)
    programs = []
    now = timems ()
    for item in result:
        measure = texts ['minutes']
        date = round ((now - item ['date']) / 60000)
        if date > 90:
            measure = texts ['hours']
            date = round (date / 60)
        if date > 36:
            measure = texts ['days']

            date = round (date / 24)

        programs.append ({'id': item ['id'], 'code': item ['code'], 'date': texts ['ago-1'] + ' ' + str (date) + ' ' + measure + ' ' + texts ['ago-2'], 'level': item ['level'], 'name': item ['name']})

    return render_template('programs.html', lang=requested_lang(), menu=render_main_menu('programs'), texts=texts, auth=TRANSLATIONS.data [lang] ['Auth'], programs=programs, username=username, current_page='programs', query_lang=query_lang, from_user=from_user)

# @app.route('/post/', methods=['POST'])
# for now we do not need a post but I am leaving it in for a potential future

# routing to index.html
@app.route('/hedy', methods=['GET'], defaults={'level': 1, 'step': 1})
@app.route('/hedy/<level>', methods=['GET'], defaults={'step': 1})
@app.route('/hedy/<level>/<step>', methods=['GET'])
def index(level, step):
    session_id()  # Run this for the side effect of generating a session ID
    g.level = level = int(level)
    g.lang = requested_lang()
    g.prefix = '/hedy'

    # If step is a string that has more than two characters, it must be an id of a program
    if step and type_check (step, 'str') and len (step) > 2:
        result = db_get ('programs', {'id': step})
        if not result:
            return 'No such program', 404
        # Allow only the owner of the program, the admin user and the teacher users to access the program
        user = current_user (request)
        if user ['username'] != result ['username'] and not is_admin (request) and not is_teacher (request):
            return 'No such program!', 404
        loaded_program = result ['code']
        # We default to step 1 to provide a meaningful default assignment
        step = 1
    else:
        loaded_program = None

    return hedyweb.render_assignment_editor(
        request=request,
        course=HEDY_COURSE[g.lang],
        level_number=level,
        assignment_number=step,
        menu=render_main_menu('hedy'),
        translations=TRANSLATIONS,
        version=version(),
        loaded_program=loaded_program)

@app.route('/onlinemasters', methods=['GET'], defaults={'level': 1, 'step': 1})
@app.route('/onlinemasters/<level>', methods=['GET'], defaults={'step': 1})
@app.route('/onlinemasters/<level>/<step>', methods=['GET'])
def onlinemasters(level, step):
    session_id()  # Run this for the side effect of generating a session ID
    g.level = level = int(level)
    g.lang = lang = requested_lang()
    g.prefix = '/onlinemasters'

    return hedyweb.render_assignment_editor(
        request=request,
        course=ONLINE_MASTERS_COURSE,
        level_number=level,
        assignment_number=step,
        translations=TRANSLATIONS,
        version=version(),
        menu=None,
        loaded_program=None)

@app.route('/space_eu', methods=['GET'], defaults={'level': 1, 'step': 1})
@app.route('/space_eu/<level>', methods=['GET'], defaults={'step': 1})
@app.route('/space_eu/<level>/<step>', methods=['GET'])
def space_eu(level, step):
    session_id()  # Run this for the side effect of generating a session ID
    g.level = level = int(level)
    g.lang = requested_lang()
    g.prefix = '/space_eu'

    return hedyweb.render_assignment_editor(
        request=request,
        course=SPACE_EU_COURSE[g.lang],
        level_number=level,
        assignment_number=step,
        translations=TRANSLATIONS,
        version=version(),
        menu=None,
        loaded_program=None)



@app.route('/error_messages.js', methods=['GET'])
def error():
    error_messages = TRANSLATIONS.get_translations(requested_lang(), "ClientErrorMessages")
    return render_template("error_messages.js", error_messages=json.dumps(error_messages))


@app.errorhandler(500)
def internal_error(exception):
    import traceback
    print(traceback.format_exc())
    return "<h1>500 Internal Server Error</h1>"

@app.route('/index.html')
@app.route('/')
def default_landing_page():
    return main_page('start')

@app.route('/<page>')
def main_page(page):
    if page == 'favicon.ico':
        abort(404)

    lang = requested_lang()
    effective_lang = lang

    if page in ['signup', 'login', 'my-profile', 'recover', 'reset', 'admin']:
        return auth_templates(page, lang, render_main_menu(page), request)

    if page == 'programs':
        return programs_page(request)

    # Default to English if requested language is not available
    if not path.isfile(f'main/{page}-{effective_lang}.md'):
        effective_lang = 'en'

    try:
        with open(f'main/{page}-{effective_lang}.md', 'r', encoding='utf8') as f:
            contents = f.read()
    except IOError:
        abort(404)

    front_matter, markdown = split_markdown_front_matter(contents)

    menu = render_main_menu(page)
    return render_template('main-page.html', mkd=markdown, lang=lang, menu=menu, username=current_user(request) ['username'], auth=TRANSLATIONS.data [lang] ['Auth'], **front_matter)


def session_id():
    """Returns or sets the current session ID."""
    if os.getenv('IS_TEST_ENV') and 'x-session_id' in request.headers:
        return request.headers['x-session_id']
    if 'session_id' not in session:
        session['session_id'] = uuid.uuid4().hex
    return session['session_id']


def requested_lang():
    """Return the user's requested language code.

    If not in the request parameters, use the browser's accept-languages
    header to do language negotiation.
    """
    lang = request.args.get("lang")
    if lang: return lang

    return request.accept_languages.best_match(ALL_LANGUAGES.keys(), 'en')

@app.template_global()
def current_language():
    return make_lang_obj(requested_lang())

@app.template_global()
def hedy_link(level_nr, assignment_nr, subpage=None, lang=None):
    """Make a link to a Hedy page."""
    parts = [g.prefix]
    parts.append('/' + str(level_nr))
    if str(assignment_nr) != '1' or subpage:
        parts.append('/' + str(assignment_nr if assignment_nr else '1'))
    if subpage and subpage != 'code':
        parts.append('/' + subpage)
    parts.append('?')
    parts.append('lang=' + (lang if lang else requested_lang()))
    return ''.join(parts)

@app.template_global()
def other_languages():
    cl = requested_lang()
    return [make_lang_obj(l) for l in ALL_LANGUAGES.keys() if l != cl]


def make_lang_obj(lang):
    """Make a language object for a given language."""
    return {
        'sym': ALL_LANGUAGES[lang],
        'lang': lang
    }


@app.template_global()
def modify_query(**new_values):
    args = request.args.copy()

    for key, value in new_values.items():
        args[key] = value

    return '{}?{}'.format(request.path, url_encode(args))


def no_none_sense(d):
    """Remove all None values from a dict."""
    return {k: v for k, v in d.items() if v is not None}


def version():
    """Get the version from the Heroku environment variables."""
    if not os.getenv('DYNO'):
        # Not on Heroku
        return 'DEV'

    vrz = os.getenv('HEROKU_RELEASE_CREATED_AT')
    the_date = datetime.date.fromisoformat(vrz[:10]) if vrz else datetime.date.today()

    commit = os.getenv('HEROKU_SLUG_COMMIT', '????')[0:6]
    return the_date.strftime('%b %d') + f' ({commit})'


def split_markdown_front_matter(md):
    parts = re.split('^---', md, 1, re.M)
    if len(parts) == 1:
        return {}, md
    # safe_load returns 'None' if the string is empty
    front_matter = yaml.safe_load(parts[0]) or {}
    return front_matter, parts[1]


def render_main_menu(current_page):
    """Render a list of (caption, href, selected, color) from the main menu."""
    return [dict(
        caption=item.get(requested_lang(), item.get('en', '???')),
        href='/' + item['_'],
        selected=(current_page == item['_']),
        accent_color=item.get('accent_color', 'white')
    ) for item in main_menu_json['nav']]

# *** PROGRAMS ***

# Not very restful to use a GET to delete something, but indeed convenient; we can do it with a single link and avoiding AJAX.
@app.route('/programs/delete/<program_id>', methods=['GET'])
@requires_login
def delete_program (user, program_id):
    result = db_get ('programs', {'id': program_id})
    if not result or result ['username'] != user ['username']:
        return "", 404
    db_del ('programs', {'id': program_id})
    program_count = 0
    if 'program_count' in user:
        program_count = user ['program_count']
    db_set ('users', {'username': user ['username'], 'program_count': program_count - 1})
    return redirect ('/programs')

@app.route('/programs', methods=['POST'])
@requires_login
def save_program (user):

    body = request.json
    if not type_check (body, 'dict'):
        return 'body must be an object', 400
    if not object_check (body, 'code', 'str'):
        return 'code must be a string', 400
    if not object_check (body, 'name', 'str'):
        return 'name must be a string', 400
    if not object_check (body, 'level', 'int'):
        return 'level must be an integer', 400

    # We execute the saved program to see if it would generate an error or not
    error = None
    try:
        hedy_errors = TRANSLATIONS.get_translations(requested_lang(), 'HedyErrorMessages')
        result = hedy.transpile(body ['code'], body ['level'])
    except hedy.HedyException as E:
        error_template = hedy_errors[E.error_code]
        error = error_template.format(**E.arguments)
    except Exception as E:
        error = str(E)

    name = body ['name']

    # We check if a program with a name `xyz` exists in the database for the username. If it does, we exist whether `xyz (1)` exists, until we find a program `xyz (NN)` that doesn't exist yet.
    # It'd be ideal to search by username & program name, but since DynamoDB doesn't allow searching for two indexes at the same time, this would require to create a special index to that effect, which is cumbersome.
    # For now, we bring all existing programs for the user and then search within them for repeated names.
    existing = db_get_many ('programs', {'username': user ['username']}, True)
    name_counter = 0
    for program in existing:
        if re.match ('^' + re.escape (name) + '( \(\d+\))*', program ['name']):
            name_counter = name_counter + 1
    if name_counter:
        name = name + ' (' + str (name_counter) + ')'

    db_set('programs', {
        'id': uuid.uuid4().hex,
        'session': session_id(),
        'date': timems (),
        'lang': requested_lang(),
        'version': version(),
        'level': body ['level'],
        'code': body ['code'],
        'name': name,
        'server_error': error,
        'username': user ['username']
    })
    program_count = 0
    if 'program_count' in user:
        program_count = user ['program_count']
    db_set('users', {'username': user ['username'], 'program_count': program_count + 1})

    return jsonify({})

# *** AUTH ***

import auth
auth.routes (app, requested_lang)

# *** START SERVER ***

if __name__ == '__main__':
    # Threaded option to enable multiple instances for multiple user access support
    app.run(threaded=True, port=config ['port'])
