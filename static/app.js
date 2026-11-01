const role = document.body.dataset.role;
const $ = id => document.getElementById(id);

let latest = null,
    sampledAt = 0,
    connected = false,
    editing = null,
    sending = false;

const labels = {
    waiting: 'Waiting',
    running: 'Live',
    paused: 'Paused',
    buzzed: 'Buzzer locked',
    expired: 'Time’s up'
};

async function api(path, data) {
    const response = await fetch(path, {
        method: data === undefined ? 'GET' : 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': document.querySelector('meta[name=csrf]').content
        },
        body: data === undefined ? undefined : JSON.stringify(data)
    });

    const result = await response.json();

    if (!response.ok) {
        throw Error(result.error || 'Failed.');
    }

    return result;
}

function error(message = '') {
    $('error').textContent = message;
    $('error').hidden = !message;
}

function render(state) {
    latest = state;
    sampledAt = performance.now();

    if ($('status')) {
        $('status').textContent = labels[state.status];
    }

    if ($('question')) {
        $('question').textContent = state.question || 'Waiting';
    }

    if ($('winner')) {
        $('winner').textContent = state.winner ? `${state.winner} buzzed` : '';
    }

    if ($('teams')) {
        $('teams').textContent = `Teams: ${state.teams.join(', ') || 'NULL'}`;
    }

    if ($('advance')) {
        $('advance').textContent = state.question_id ? 'Next question' : 'Start';
        $('advance').dataset.action = state.question_id ? 'next' : 'start';

        document.querySelector('[data-action=pause]').disabled = state.status !== 'running';
        document.querySelector('[data-action=resume]').disabled = state.status !== 'paused';
        document.querySelector('[data-action=reopen]').disabled = state.status !== 'buzzed';
    }

    if ($('buzz')) {
        $('buzz').disabled = !connected || state.status !== 'running' || sending;
        $('buzz').classList.toggle('won', state.status === 'buzzed');

        $('buzz').textContent =
            state.status === 'buzzed' ? state.winner + ' buzzed first' :
            state.status === 'expired' ? 'Time’s up' :
            state.status === 'paused' ? 'Paused' :
            state.status === 'waiting' ? 'Waiting' :
            'Click or Enter to trigger';
    }
}

async function poll() {
    try {
        const state = await api('/api/state?view=' + role);

        connected = true;
        $('connection').textContent = '';

        render(state);
    } catch (e) {
        connected = false;
        $('connection').textContent = 'Disconnected · reconnecting';

        if ($('buzz')) {
            $('buzz').disabled = true;
        }
    }

    setTimeout(poll, 200);
}

setInterval(() => {
    if (!latest) return;

    const left = Math.max(
        0,
        latest.remaining - (
            latest.status === 'running'
                ? (performance.now() - sampledAt) / 1000
                : 0
        )
    );

    const seconds = Math.ceil(left);

    $('timer').textContent =
        `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;

    if ($('buzz') && left <= 0) {
        $('buzz').disabled = true;
    }
}, 50);

async function loadQuestions() {
    const questions = await api('/api/admin/questions');

    $('questions').replaceChildren();

    for (const q of questions) {
        const li = document.createElement('li');
        const p = document.createElement('p');
        const span = document.createElement('span');

        p.textContent = q.text;
        span.textContent = `${q.duration}s`;

        li.append(p, span);

        for (const action of ['Edit', 'Delete']) {
            const button = document.createElement('button');

            button.className = 'secondary';
            button.textContent = action;

            button.onclick = async () => {
                if (action === 'Edit') {
                    editing = q.id;

                    $('text').value = q.text;
                    $('duration').value = q.duration;
                    $('editor-title').textContent = 'Edit question';
                    $('cancel-edit').hidden = false;

                    $('text').focus();
                } else if (confirm('Delete?')) {
                    try {
                        await api('/api/admin/delete', { id: q.id });
                        await loadQuestions();
                    } catch (e) {
                        error(e.message);
                    }
                }
            };

            li.append(button);
        }

        $('questions').append(li);
    }
}

function clearEditor() {
    editing = null;

    $('editor').reset();
    $('editor-title').textContent = 'Add question';
    $('cancel-edit').hidden = true;
}

if (role !== 'home') {
    poll();
}

if (role === 'admin') {
    loadQuestions().catch(e => error(e.message));

    $('editor').onsubmit = async event => {
        event.preventDefault();

        try {
            await api('/api/admin/questions', {
                id: editing,
                text: $('text').value,
                duration: Number($('duration').value)
            });

            clearEditor();
            await loadQuestions();
            error();
        } catch (e) {
            error(e.message);
        }
    };

    $('cancel-edit').onclick = clearEditor;

    document.querySelectorAll('[data-action]').forEach(button => {
        button.onclick = async () => {
            if (
                button.dataset.action === 'reset' &&
                !confirm('Reset?')
            ) {
                return;
            }

            button.disabled = true;

            try {
                render(
                    await api('/api/admin/control', {
                        action: button.dataset.action
                    })
                );

                error();
            } catch (e) {
                error(e.message);
            } finally {
                button.disabled = false;
            }
        };
    });
}

async function buzz() {
    if (!connected || sending || !latest || $('buzz').disabled) {
        return;
    }

    sending = true;
    $('buzz').disabled = true;

    try {
        render(
            await api('/api/buzz', {
                revision: latest.revision
            })
        );

        error();
    } catch (e) {
        error(e.message);
    } finally {
        sending = false;
    }
}

if (role === 'contestant') {
    $('buzz').onclick = buzz;

    document.addEventListener('keydown', event => {
        if (event.key === 'Enter' && !event.repeat) {
            event.preventDefault();
            buzz();
        }
    });
}