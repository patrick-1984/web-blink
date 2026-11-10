"""User-facing next steps, kept separate from transport and recording mechanics."""


def guide(stage, *, browser=None, binding=None):
    """Every hand-off names the actual state and the next user action; never a setup tutorial."""
    if stage == 'learning_pending':
        return {'stage': 'learning_pending', 'recording': False,
                'message': 'Recording is not confirmed for this step. Follow Blink’s returned next steps before doing it in the browser.',
                'agentInstruction': 'For a missing schema, start learning before any Playwright navigation, click, '
                    'evaluation or other page action. Resolve discovery, activation, account, approval, policy and '
                    'browser-consent blockers through Blink; none permits a silent plain Playwright fallback. '
                    'For learning_offer, ask permission to learn and preserve the on-request setting. '
                    'Only a direct user request not to learn permits plain browsing; do not infer that request from a blocker. '
                    'This handoff does not describe or transfer ownership of any other active recording.'}
    if stage == 'connected':
        return {'stage': 'connected_not_recording', 'recording': False,
                'message': 'The browser is ready. Recording is OFF.',
                'question': 'What would you like to do next?',
                'choices': ['Start recording this app', 'Sign in first, then start recording'],
                'agentInstruction': 'State that recording is off. Connection alone never permits an unrecorded missing-schema step. '
                    'For an ordered site task use blink_execution_run before any page action unless the user directly asked not to learn. '
                    'If the user already asked to learn/record an app, '
                    'continue with blink_start_recording in this turn using that app URL and the returned browser. '
                    'Changing the browser does not cancel that goal. Otherwise ask the question with these choices. '
                    'If the app URL is unknown, ask which app to record. If they choose signing in first, keep the app/browser '
                    'and offer “Start recording” / “Keep signing in” as the next choice. '
                    'Never end with only “you can log in now” or make them repeat the original goal.',
                'nextActions': [{'tool': 'blink_start_recording', 'params': {'browser': browser},
                                 'needs': ['url'], 'label': 'Start recording the chosen app'}]}
    if stage == 'recording':
        return {'stage': 'recording', 'recording': True,
                'message': 'Recording is ON. Sign in if needed, then demonstrate the workflow you want Blink to learn.',
                'nextUserAction': 'Show the steps in the recording tab. Say “finished” when you are done.',
                'agentInstruction': 'Say recording is on only after the tool confirms it. Give this one next step, '
                    'then wait for the user to demonstrate. Do not drive the browser unless asked. '
                    'On “finished”, stop recording and continue the user’s original learn/build goal.',
                'nextActions': [{'tool': 'blink_request_approval', 'method': 'learn_stop',
                                 'params': {'binding': binding}, 'when': 'user_finished', 'label': 'Finish recording'}]}
    if stage == 'on_the_way':
        return {'stage': 'learning_on_the_way', 'recording': True,
                'message': 'Recording is ON. Blink has no operation for this step yet, so it is learning it while the task is done.',
                'nextUserAction': 'Sign in in the recording tab if asked; otherwise nothing is needed.',
                'agentInstruction': 'Do the ordered step yourself in Blink’s recording tab (only that owned tab; never tab 0 or '
                    'other sessions’ tabs). Blink opened it through its own browser connection, so your browser tool still '
                    'points at your previous tab: select the recording tab (browser_tabs select) before browser_evaluate or '
                    'any page action; requests made in another tab are not recorded. Hand the tab to the user only for sign-in or when you cannot reach it, then continue. '
                    'Never enter data the order does not give. When the step is done: learn_stop, learn_propose, activate_blink, '
                    'test the Blink with this use case (blink_verify for reads with recorded arguments and values, '
                    'execution_observe for writes already done, never replay them), then continue the remaining steps with '
                    'blink_execution_run. A later missing operation repeats this cycle.',
                'nextActions': [{'tool': 'blink_request_approval', 'method': 'learn_stop',
                                 'params': {'binding': binding}, 'when': 'step_done', 'label': 'Finish recording this step'}]}
    if stage == 'stopped':
        return {'stage': 'recording_finished', 'recording': False,
                'message': 'Recording has finished. Your captured session is saved locally.',
                'question': 'Shall I turn this recording into a Blink, or would you like to review it first?',
                'choices': ['Build the Blink', 'Review the recording'],
                'agentInstruction': 'If the user already requested learning/building a Blink, continue proposal and activation '
                    'in this turn without asking again. If the request was recording only, ask this next-step question.'}
    if stage == 'recording_problem':
        return {'stage': 'recording_needs_attention', 'recording': True,
                'message': 'A recording session exists, but it is not saving correctly.',
                'question': 'Try to recover this recording, or finish and keep what was saved?',
                'choices': ['Recover the recording', 'Finish and keep the saved data'],
                'agentInstruction': 'Explain the reported problem briefly. Do not claim healthy recording or start a duplicate. '
                    'Use Blink status/recovery tools; never ask for shell port diagnostics.'}
    if stage == 'other_recordings':
        return {'stage': 'recording_in_another_conversation', 'recording': False,
                'message': 'This conversation has not started a recording. A recording is active in another Blink conversation.',
                'question': 'Check the existing recording, or start a separate one?',
                'choices': ['Check the existing recording', 'Start a separate recording'],
                'agentInstruction': 'Do not claim all recording is off or silently take over the other session. '
                    'For an explicitly chosen existing recording, call blink_browser_status with its binding.'}
    if stage == 'choose_browser':
        return {'stage': 'choose_browser', 'recording': False,
                'message': 'Choose the browser session you want to use.',
                'question': 'Use one of your existing profiles, or open a separate browser for Blink?',
                'choices': ['Use an existing profile', 'Open a separate browser'],
                'agentInstruction': 'For an existing profile show the numbered browser/profile table and accept #IDs. '
                    'For a separate browser prepare it yourself. Preserve any existing learn/record request.'}
    raise ValueError('Unknown onboarding stage')
