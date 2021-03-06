#!/usr/bin/env python3
import time
from common.op_params import opParams
import ast
import difflib


class STYLES:
  # HEADER = '\033[95m'
  # OKBLUE = '\033[94m'
  # CBLUE = '\33[44m'
  # BOLD = '\033[1m'
  OKGREEN = '\033[92m'
  CWHITE = '\33[37m'
  ENDC = '\033[0m' + CWHITE
  UNDERLINE = '\033[4m'

  RED = '\033[91m'
  PURPLE_BG = '\33[45m'
  YELLOW = '\033[93m'

  FAIL = RED
  INFO = PURPLE_BG
  SUCCESS = OKGREEN
  PROMPT = YELLOW
  CYAN = '\033[36m'


class opEdit:  # use by running `python /data/openpilot/op_edit.py`
  def __init__(self):
    self.op_params = opParams()
    self.params = None
    self.sleep_time = 0.75
    self.live_tuning = self.op_params.get('op_edit_live_mode', False)
    self.username = self.op_params.get('username', None)

    self.last_choice = None

    self.run_init()

  def run_init(self):
    if self.username is None:
      self.success('\nWelcome to the opParams command line editor!', sleep_time=0)
      self.prompt('Parameter \'username\' is missing! Would you like to add your Discord username for easier crash debugging?')

      username_choice = self.input_with_options(['Y', 'n', 'don\'t ask again'], default='n')[0]
      if username_choice == 0:
        self.prompt('Please enter your Discord username so the developers can reach out if a crash occurs:')
        username = ''
        while username == '':
          username = input('>> ').strip()
        self.success('Thanks! Saved your Discord username\n'
                     'Edit the \'username\' parameter at any time to update', sleep_time=3.0)
        self.op_params.put('username', username)
        self.username = username
      elif username_choice == 2:
        self.op_params.put('username', False)
        self.info('Got it, bringing you into opEdit\n'
                  'Edit the \'username\' parameter at any time to update', sleep_time=3.0)
    else:
      self.success('\nWelcome to the opParams command line editor, {}!'.format(self.username), sleep_time=0)

    self.run_loop()

  def run_loop(self):
    while True:
      if not self.live_tuning:
        self.info('Here are your parameters:', end='\n', sleep_time=0)
      else:
        self.info('Here are your live parameters:', end='\n', sleep_time=0)
      self.params = self.op_params.get(force_update=True)
      if self.live_tuning:  # only display live tunable params
        self.params = {k: v for k, v in self.params.items() if self.op_params.key_info(k).live}

      values_list = [self.params[i] if len(str(self.params[i])) < 20 else '{} ... {}'.format(str(self.params[i])[:30], str(self.params[i])[-15:]) for i in self.params]
      live = ['(live!)' if self.op_params.key_info(i).live else '' for i in self.params]

      to_print = []
      for idx, param in enumerate(self.params):
        line = '{}. {}: {}  {}'.format(idx + 1, param, values_list[idx], live[idx])
        if idx == self.last_choice and self.last_choice is not None:
          line = STYLES.OKGREEN + line
        else:
          line = STYLES.CYAN + line
        to_print.append(line)

      extras = {'a': 'Add new parameter',
                'd': 'Delete parameter',
                'l': 'Toggle live tuning',
                'e': 'Exit opEdit'}

      to_print += ['---'] + ['{}. {}'.format(e, extras[e]) for e in extras]
      print('\n'.join(to_print))
      self.prompt('\nChoose a parameter to edit (by index or name):')

      choice = input('>> ').strip()
      parsed, choice = self.parse_choice(choice, len(to_print) - len(extras))
      if parsed == 'continue':
        continue
      elif parsed == 'add':
        self.add_parameter()
      elif parsed == 'change':
        self.last_choice = choice
        self.change_parameter(choice)
      elif parsed == 'delete':
        self.delete_parameter()
      elif parsed == 'live':
        self.last_choice = None
        self.live_tuning = not self.live_tuning
        self.op_params.put('op_edit_live_mode', self.live_tuning)  # for next opEdit startup
      elif parsed == 'exit':
        return

  def parse_choice(self, choice, opt_len):
    if choice.isdigit():
      choice = int(choice)
      choice -= 1
      if choice not in range(opt_len):  # number of options to choose from
        self.error('Not in range!')
        return 'continue', choice
      return 'change', choice

    if choice in ['a', 'add']:  # add new parameter
      return 'add', choice
    elif choice in ['d', 'delete', 'del']:  # delete parameter
      return 'delete', choice
    elif choice in ['l', 'live']:  # live tuning mode
      return 'live', choice
    elif choice in ['exit', 'e', '']:
      self.error('Exiting opEdit!', sleep_time=0)
      return 'exit', choice
    else:  # find most similar param to user's input
      param_sims = [(idx, self.str_sim(choice, param)) for idx, param in enumerate(self.params)]
      param_sims = [param for param in param_sims if param[1] > 0.5]
      if len(param_sims) > 0:
        chosen_param = sorted(param_sims, key=lambda param: param[1], reverse=True)[0]
        return 'change', chosen_param[0]  # return idx

    self.error('Invalid choice!')
    return 'continue', choice

  def str_sim(self, a, b):
    return difflib.SequenceMatcher(a=a, b=b).ratio()

  def change_parameter(self, choice):
    while True:
      chosen_key = list(self.params)[choice]
      key_info = self.op_params.key_info(chosen_key)

      old_value = self.params[chosen_key]
      self.info('Chosen parameter: {}'.format(chosen_key), sleep_time=0)

      to_print = []
      if key_info.has_description:
        to_print.append('>>  Description: {}'.format(self.op_params.default_params[chosen_key]['description'].replace('\n', '\n  > ')))
      if key_info.has_allowed_types:
        to_print.append('>>  Allowed types: {}'.format(', '.join([i.__name__ for i in key_info.allowed_types])))
      if key_info.live:
        to_print.append('>>  This parameter supports live tuning! Updates should take affect within 5 seconds')

      if to_print:
        print('\n{}\n'.format('\n'.join(to_print)))

      if key_info.is_list:
        self.change_param_list(old_value, key_info, chosen_key)  # TODO: need to merge the code in this function with the below to reduce redundant code
        return

      self.info('Current value: {} (type: {})'.format(old_value, type(old_value).__name__), sleep_time=0)

      while True:
        self.prompt('\nEnter your new value:')
        new_value = input('>> ').strip()
        if new_value == '':
          self.info('Exiting this parameter...', 0.5)
          return

        new_value = self.str_eval(new_value)
        if key_info.has_allowed_types and type(new_value) not in key_info.allowed_types:
          self.error('The type of data you entered ({}) is not allowed with this parameter!'.format(type(new_value).__name__))
          continue

        if key_info.live:  # stay in live tuning interface
          self.op_params.put(chosen_key, new_value)
          self.success('Saved {} with value: {}! (type: {})'.format(chosen_key, new_value, type(new_value).__name__))
        else:  # else ask to save and break
          print('\nOld value: {} (type: {})'.format(old_value, type(old_value).__name__))
          print('New value: {} (type: {})'.format(new_value, type(new_value).__name__))
          self.prompt('\nDo you want to save this?')
          if self.input_with_options(['Y', 'n'], 'n')[0] == 0:
            self.op_params.put(chosen_key, new_value)
            self.success('Saved!')
          else:
            self.info('Not saved!', sleep_time=0)
          return

  def change_param_list(self, old_value, key_info, chosen_key):
    while True:
      self.info('Current value: {} (type: {})'.format(old_value, type(old_value).__name__), sleep_time=0)
      self.prompt('\nEnter index to edit (0 to {}):'.format(len(old_value) - 1))
      choice_idx = self.str_eval(input('>> '))
      if choice_idx == '':
        self.info('Exiting this parameter...', 0.5)
        return

      if not isinstance(choice_idx, int) or choice_idx not in range(len(old_value)):
        self.error('Must be an integar within list range!')
        continue

      while True:
        self.info('Chosen index: {}'.format(choice_idx), sleep_time=0)
        self.info('Value: {} (type: {})'.format(old_value[choice_idx], type(old_value[choice_idx]).__name__), sleep_time=0)
        self.prompt('\nEnter your new value:')
        new_value = input('>> ').strip()
        if new_value == '':
          self.info('Exiting this list item...', 0.5)
          break

        new_value = self.str_eval(new_value)
        if key_info.has_allowed_types and type(new_value) not in key_info.allowed_types:
          self.error('The type of data you entered ({}) is not allowed with this parameter!'.format(type(new_value).__name__))
          continue

        old_value[choice_idx] = new_value

        self.op_params.put(chosen_key, old_value)
        self.success('Saved {} with value: {}! (type: {})'.format(chosen_key, new_value, type(new_value).__name__), end='\n')
        break

  def cyan(self, msg, end=''):
    msg = self.str_color(msg, style='cyan')
    # print(msg, flush=True, end='\n' + end)
    return msg

  def prompt(self, msg, end=''):
    msg = self.str_color(msg, style='prompt')
    print(msg, flush=True, end='\n' + end)

  def info(self, msg, sleep_time=None, end=''):
    if sleep_time is None:
      sleep_time = self.sleep_time
    msg = self.str_color(msg, style='info')

    print(msg, flush=True, end='\n' + end)
    time.sleep(sleep_time)

  def error(self, msg, sleep_time=None, end='', surround=True):
    if sleep_time is None:
      sleep_time = self.sleep_time
    msg = self.str_color(msg, style='fail', surround=surround)

    print(msg, flush=True, end='\n' + end)
    time.sleep(sleep_time)

  def success(self, msg, sleep_time=None, end=''):
    if sleep_time is None:
      sleep_time = self.sleep_time
    msg = self.str_color(msg, style='success', surround=False, underline=False)

    print(msg, flush=True, end='\n' + end)
    time.sleep(sleep_time)

  def str_color(self, msg, style, surround=False, underline=False):
    if style == 'success':
      style = STYLES.SUCCESS
    elif style == 'fail':
      style = STYLES.FAIL
    elif style == 'prompt':
      style = STYLES.PROMPT
    elif style == 'info':
      style = STYLES.INFO
    elif style == 'cyan':
      style = STYLES.CYAN

    if underline:
      underline = STYLES.UNDERLINE
    else:
      underline = ''

    if surround:
      msg = '{}--------\n{}{}\n{}--------{}'.format(style, underline, msg, STYLES.ENDC + style, STYLES.ENDC)
    else:
      msg = '{}{}{}'.format(style + underline, msg, STYLES.ENDC)

    return msg

  def input_with_options(self, options, default=None):
    """
    Takes in a list of options and asks user to make a choice.
    The most similar option list index is returned along with the similarity percentage from 0 to 1
    """
    user_input = input('[{}]: '.format('/'.join(options))).lower().strip()
    if not user_input:
      return default, 0.0
    sims = [self.str_sim(i.lower().strip(), user_input) for i in options]
    argmax = sims.index(max(sims))
    return argmax, sims[argmax]

  def str_eval(self, dat):
    dat = dat.strip()
    try:
      dat = ast.literal_eval(dat)
    except:
      if dat.lower() == 'none':
        dat = None
      elif dat.lower() == 'false':
        dat = False
      elif dat.lower() == 'true':  # else, assume string
        dat = True
    return dat

  def delete_parameter(self):
    while True:
      self.prompt('Enter the name of the parameter to delete:')
      key = self.str_eval(input('>> '))

      if key == '':
        return
      if not isinstance(key, str):
        self.error('Input must be a string!')
        continue
      if key not in self.params:
        self.error("Parameter doesn't exist!")
        continue

      value = self.params.get(key)
      print('Parameter name: {}'.format(key))
      print('Parameter value: {} (type: {})'.format(value, type(value).__name__))
      self.prompt('Do you want to delete this?')

      if self.input_with_options(['Y', 'n'], default='n')[0] == 0:
        self.op_params.delete(key)
        self.success('Deleted!')
      else:
        self.info('Not deleted!')
      return

  def add_parameter(self):
    while True:
      self.prompt('Type the name of your new parameter:')
      key = self.str_eval(input('>> '))

      if key == '':
        return
      if not isinstance(key, str):
        self.error('Input must be a string!')
        continue

      self.prompt("Enter the data you'd like to save with this parameter:")
      value = input('>> ').strip()
      value = self.str_eval(value)

      print('Parameter name: {}'.format(key))
      print('Parameter value: {} (type: {})'.format(value, type(value).__name__))
      self.prompt('Do you want to save this?')

      if self.input_with_options(['Y', 'n'], default='n')[0] == 0:
        self.op_params.put(key, value)
        self.success('Saved!')
      else:
        self.info('Not saved!')
      return


opEdit()
