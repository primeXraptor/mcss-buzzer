import logging
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request, session


def create_app(test_config=None):
    app = Flask(__name__)
    app.url_map.redirect_defaults = False
    app.logger.setLevel(logging.INFO)

    app.config.update(
        SECRET_KEY=os.environ.get('SECRET_KEY') or secrets.token_hex(32),
        DATABASE=str(Path(app.instance_path) / 'quiz.sqlite'),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Strict',
        MAX_CONTENT_LENGTH=1024 * 1024
    )

    if test_config:
        app.config.update(test_config)

    Path(app.config['DATABASE']).parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(app.config['DATABASE']) as db:
        db.execute(
            'CREATE TABLE IF NOT EXISTS questions ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'text TEXT NOT NULL, '
            'duration INTEGER NOT NULL)'
        )

    lock = threading.RLock()

    state = dict(
        status='waiting',
        current=None,
        deadline=None,
        remaining=0,
        winner=None,
        teams={'1': 'Maliyadeva Balika College', '2': 'Holy Family Balika College'},
        revision=0
    )

    def questions():
        with sqlite3.connect(app.config['DATABASE']) as db:
            return [
                dict(id=r[0], text=r[1], duration=r[2])
                for r in db.execute('SELECT * FROM questions ORDER BY id')
            ]

    def expire():
        if (
            state['status'] == 'running'
            and time.monotonic() >= state['deadline']
        ):
            state.update(
                status='expired',
                remaining=0,
                deadline=None
            )

    def snapshot():
        expire()

        left = (
            max(0, state['deadline'] - time.monotonic())
            if state['deadline']
            else state['remaining']
        )

        current = state['current']

        data = dict(
            status=state['status'],
            remaining=left,
            winner=state['winner'],
            question_id=current['id'] if current else None,
            revision=state['revision']
        )

        if request.args.get('view') != 'contestant':
            data['question'] = current['text'] if current else ''

        if request.args.get('view') == 'admin':
            data['teams'] = list(state['teams'].values())

        return data

    @app.before_request
    def protect():
        if request.method == 'POST':
            if (
                not session.get('csrf')
                or not secrets.compare_digest(
                    session['csrf'],
                    request.headers.get('X-CSRF-Token', '')
                )
            ):
                return jsonify(error='Reload the page and try again.'), 403

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'

        # Successful state polling happens five times per second per client.
        if request.path != '/api/state' or response.status_code >= 400:
            app.logger.info(
                '%s %s -> %s role=%s seat=%s',
                request.method, request.path, response.status_code,
                session.get('role', 'guest'), session.get('team_id', '-')
            )

        return response

    @app.get('/')
    def home():
        session.setdefault('csrf', secrets.token_hex(24))

        return render_template('index.html', role='home')

    @app.get(
        '/contestant/<int:seat>',
        defaults={'role': 'contestant'}
    )
    @app.get('/<role>', defaults={'seat': 1})
    def page(role, seat):
        if role not in ('admin', 'contestant', 'wall', 'announcer'):
            return 'Not found', 404

        session.setdefault('csrf', secrets.token_hex(24))
        session['role'] = role

        if role == 'contestant':
            if seat not in (1, 2):
                return 'Not found', 404

            session['team_id'] = str(seat)

        return render_template('index.html', role=role)

    @app.get('/api/state')
    def get_state():
        if session.get('role') not in (
            'admin',
            'contestant',
            'wall',
            'announcer'
        ):
            return jsonify(error='Join first.'), 403

        with lock:
            return jsonify(snapshot())

    @app.get('/api/admin/questions')
    def list_questions():
        return jsonify(questions())

    @app.post('/api/admin/questions')
    def save_question():
        data = request.get_json() or {}

        text = str(data.get('text', '')).strip()
        duration = data.get('duration')

        if (
            not text
            or len(text) > 5000
            or type(duration) is not int
            or not 1 <= duration <= 3600
        ):
            return jsonify(
                error='Enter a question and a timer of 1–3600 seconds.'
            ), 400

        with lock, sqlite3.connect(app.config['DATABASE']) as db:
            if data.get('id'):
                if not db.execute(
                    'UPDATE questions SET text=?, duration=? WHERE id=?',
                    (text, duration, data['id'])
                ).rowcount:
                    return jsonify(error='Question not found.'), 404
            else:
                db.execute(
                    'INSERT INTO questions(text,duration) VALUES (?,?)',
                    (text, duration)
                )

        return jsonify(ok=True)

    @app.post('/api/admin/delete')
    def delete_question():
        with lock, sqlite3.connect(app.config['DATABASE']) as db:
            db.execute(
                'DELETE FROM questions WHERE id=?',
                ((request.get_json() or {}).get('id'),)
            )

        return jsonify(ok=True)

    @app.post('/api/admin/control')
    def control():
        action = (request.get_json() or {}).get('action')

        with lock:
            expire()

            if action in ('start', 'next'):
                current_id = (
                    state['current']['id']
                    if state['current']
                    else 0
                )

                following = next(
                    (
                        q for q in questions()
                        if q['id'] > current_id
                    ),
                    None
                )

                if not following:
                    return jsonify(
                        error='No more questions. Add a question or reset the quiz.'
                    ), 409

                state.update(
                    current=following,
                    status='running',
                    deadline=time.monotonic() + following['duration'],
                    remaining=following['duration'],
                    winner=None
                )

            elif action == 'pause' and state['status'] == 'running':
                state.update(
                    remaining=max(
                        0,
                        state['deadline'] - time.monotonic()
                    ),
                    deadline=None,
                    status='paused'
                )

            elif action == 'resume' and state['status'] == 'paused':
                state.update(
                    deadline=time.monotonic() + state['remaining'],
                    status='running'
                )

            elif (
                action == 'reopen'
                and state['status'] == 'buzzed'
                and state['remaining'] > 0
            ):
                state.update(
                    deadline=time.monotonic() + state['remaining'],
                    status='running',
                    winner=None
                )

            elif action == 'reset':
                state.update(
                    status='waiting',
                    current=None,
                    deadline=None,
                    remaining=0,
                    winner=None
                )

            else:
                return jsonify(
                    error='That action is unavailable.'
                ), 409

            state['revision'] += 1
            app.logger.info(
                'Quiz action=%s status=%s revision=%s',
                action, state['status'], state['revision']
            )

            return jsonify(snapshot())

    @app.post('/api/buzz')
    def buzz():
        with lock:
            expire()

            if session.get('team_id') not in state['teams']:
                return jsonify(
                    error='Join as a team first.'
                ), 403

            data = request.get_json() or {}

            if (
                state['status'] != 'running'
                or data.get('revision') != state['revision']
            ):
                app.logger.info(
                    'Buzz rejected seat=%s status=%s revision=%s current_revision=%s',
                    session['team_id'], state['status'],
                    data.get('revision'), state['revision']
                )
                return jsonify(error='Buzzer is closed.'), 409

            state.update(
                remaining=max(
                    0,
                    state['deadline'] - time.monotonic()
                ),
                deadline=None,
                status='buzzed',
                winner=state['teams'][session['team_id']]
            )

            app.logger.info(
                'Buzz accepted seat=%s winner=%s remaining=%.3fs',
                session['team_id'], state['winner'], state['remaining']
            )
            return jsonify(snapshot())

    return app


if __name__ == '__main__':
    from waitress import serve

    app = create_app()

    app.logger.info('Server listening on 0.0.0.0:5000')

    serve(
        app,
        host='0.0.0.0',
        port=5000,
        threads=12
    )
