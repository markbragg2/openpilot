import numpy as np
import cereal.messaging as messaging
from common.realtime import sec_since_boot
from selfdrive.controls.lib.drive_helpers import MPC_COST_LONG
from common.op_params import opParams
from common.numpy_fast import interp, clip
from common.travis_checker import travis
from selfdrive.config import Conversions as CV
from selfdrive.controls.lib.dynamic_follow.auto_df import predict
from selfdrive.controls.df_alert_manager import dfAlertManager
from selfdrive.controls.lib.dynamic_follow.support import LeadData, CarData, dfData, dfProfiles
from common.data_collector import DataCollector
from cereal.messaging import SubMaster


class DynamicFollow:
  def __init__(self, mpc_id):
    self.mpc_id = mpc_id
    self.op_params = opParams()
    self.df_profiles = dfProfiles()
    self.df_alert_manager = dfAlertManager(self.op_params)
    self.setup_collector()
    self.default_TR = 1.8
    self.predict_rate = 1 / 5.

    if not travis and mpc_id == 1:
      self.pm = messaging.PubMaster(['dynamicFollowData'])
    else:
      self.pm = None

    self.scales = {'v_ego': [-0.06112159043550491, 37.96522521972656], 'a_lead': [-2.982128143310547, 3.3612186908721924], 'v_lead': [0.0, 35.27671432495117], 'x_lead': [2.4600000381469727, 139.52000427246094]}
    self.mpc_rate = 1 / 20.
    self.split_every = 3
    self.model_input_len = 200 * self.split_every

    self.v_lead_retention = 2.0  # keep only last x seconds
    self.v_ego_retention = 2.5
    self.setup_changing_variables()

  def setup_collector(self):
    self.sm = SubMaster(['liveTracks'])
    self.data_collector = DataCollector(file_path='/data/df_data', keys=['v_ego', 'a_lead', 'v_lead', 'x_lead', 'live_tracks', 'profile', 'time'])

  def setup_changing_variables(self):
    self.TR = self.default_TR
    self.df_profile, df_changed, change_time = self.df_alert_manager.update()
    self.df_pred = self.df_profile

    self.sng = False
    self.car_data = CarData()
    self.lead_data = LeadData()
    self.df_data = dfData()  # dynamic follow data

    self.last_cost = 0.0
    self.last_predict_time = 0.0
    self.auto_df_model_data = []

  def gather_data(self):
    self.sm.update(0)
    live_tracks = [[i.dRel, i.vRel, i.aRel, i.yRel] for i in self.sm['liveTracks']]
    if self.CS.cruiseState.enabled and self.mpc_id == 1:
      self.data_collector.update([self.car_data.v_ego,
                                  self.lead_data.a_lead,
                                  self.lead_data.v_lead,
                                  self.lead_data.x_lead,
                                  live_tracks,
                                  self.df_profile,
                                  sec_since_boot()])

  def update(self, CS, libmpc):
    self.CS = CS
    self.gather_data()
    self.update_car()
    if self.mpc_id == 1:
      self.df_profile, df_changed, change_time = self.df_alert_manager.update()  # could output profile from button or prediction if in auto
      if self.df_alert_manager.is_auto and self.lead_data.status:
        self._get_pred()

    if not self.lead_data.status or travis:
      self.TR = self.default_TR
    else:
      self._store_df_data()
      self.TR = self._get_TR()

    if not travis:
      self._change_cost(libmpc)
      self._send_cur_state()

  def _norm(self, x, name):
    self.x = x
    return np.interp(x, self.scales[name], [0, 1])

  def _send_cur_state(self):
    if self.mpc_id == 1 and self.pm is not None:
      dat = messaging.new_message()
      dat.init('dynamicFollowData')
      dat.dynamicFollowData.mpcTR = self.TR
      dat.dynamicFollowData.profilePred = self.df_pred
      self.pm.send('dynamicFollowData', dat)

  def _change_cost(self, libmpc):
    TRs = [0.9, 1.8, 2.7]
    costs = [1.0, 0.13, 0.05]
    cost = interp(self.TR, TRs, costs)
    if self.last_cost != cost:
      libmpc.change_tr(MPC_COST_LONG.TTC, cost, MPC_COST_LONG.ACCELERATION, MPC_COST_LONG.JERK)
      self.last_cost = cost

  def _store_df_data(self):
    cur_time = sec_since_boot()

    # Store lead velocity for better decision between cur lead accel and lead accel over time
    if self.lead_data.status:
      if self.lead_data.new_lead:
        self.df_data.v_leads = []  # reset when new lead
      else:
        self.df_data.v_leads = self._remove_old_entries(self.df_data.v_leads, cur_time, self.v_lead_retention)
      self.df_data.v_leads.append({'v_lead': self.lead_data.v_lead, 'time': cur_time})

    # Store our velocity for better sng
    self.df_data.v_egos = self._remove_old_entries(self.df_data.v_egos, cur_time, self.v_ego_retention)
    self.df_data.v_egos.append({'v_ego': self.car_data.v_ego, 'time': cur_time})

    # Store data for auto-df model
    self.auto_df_model_data.append([self._norm(self.car_data.v_ego, 'v_ego'),
                                    self._norm(self.lead_data.a_lead, 'a_lead'),
                                    self._norm(self.lead_data.v_lead, 'v_lead'),
                                    self._norm(self.lead_data.x_lead, 'x_lead')])
    while len(self.auto_df_model_data) > self.model_input_len:
      del self.auto_df_model_data[0]

  def _remove_old_entries(self, lst, cur_time, retention):
    return [sample for sample in lst if cur_time - sample['time'] <= retention]

  def _calculate_lead_accel(self):
    min_consider_time = 1.0  # minimum amount of time required to consider calculation
    a_lead = self.lead_data.a_lead
    if len(self.df_data.v_leads):  # if not empty
      elapsed = self.df_data.v_leads[-1]['time'] - self.df_data.v_leads[0]['time']
      if elapsed > min_consider_time:  # if greater than min time (not 0)
        a_calculated = (self.df_data.v_leads[-1]['v_lead'] - self.df_data.v_leads[0]['v_lead']) / elapsed  # delta speed / delta time
        if a_lead * a_calculated > 0 and abs(a_calculated) > abs(a_lead):
          # both are negative or positive and calculated is greater than current
          return a_calculated
        if a_calculated < 0 <= a_lead:  # accel over time is negative and current accel is zero or positive
          if a_lead < -a_calculated * 0.55:
            # half of accel over time is less than current positive accel, we're not decelerating after long decel
            return a_calculated
        if a_lead <= 0 < a_calculated:  # accel over time is positive and current accel is zero or negative
          if -a_lead < a_calculated * 0.45:
            # half of accel over time is greater than current negative accel, we're not accelerating after long accel
            return a_calculated

    return a_lead  # if above doesn't execute, we'll return measured a_lead

  def _get_pred(self):
    cur_time = sec_since_boot()
    if cur_time - self.last_predict_time > self.predict_rate:
      if len(self.auto_df_model_data) == self.model_input_len:
        pred = predict(np.array(self.auto_df_model_data[::self.split_every], dtype=np.float32).flatten())
        self.df_pred = int(np.argmax(pred))
        self.last_predict_time = cur_time

  def _get_TR(self):
    return self.op_params.get('TR', 1.8)
    x_vel = [0.0, 1.8627, 3.7253, 5.588, 7.4507, 9.3133, 11.5598, 13.645, 22.352, 31.2928, 33.528, 35.7632, 40.2336]  # velocities
    profile_mod_x = [2.2352, 13.4112, 24.5872, 35.7632]  # profile mod speeds, mph: [5., 30., 55., 80.]

    if self.df_profile == self.df_profiles.roadtrip:
      y_dist = [1.3978, 1.4071, 1.4194, 1.4348, 1.4596, 1.4904, 1.5362, 1.5565, 1.5845, 1.6205, 1.6565, 1.6905, 1.7435]  # TRs
      profile_mod_pos = [0.98, 0.915, 0.83, 0.55]
      profile_mod_neg = [1.0575, 1.18, 1.39, 1.825]
    elif self.df_profile == self.df_profiles.traffic:  # for in congested traffic
      x_vel = [0.0, 1.892, 3.7432, 5.8632, 8.0727, 10.7301, 14.343, 17.6275, 22.4049, 28.6752, 34.8858, 40.35]
      y_dist = [1.3781, 1.3791, 1.3802, 1.3825, 1.3984, 1.4249, 1.4194, 1.3162, 1.1916, 1.0145, 0.9855, 0.9562]
      profile_mod_pos = [1.05, 1.375, 2.99, 3.8]
      profile_mod_neg = [0.79, .1, 0.0, 0.0]
    else:  # default to relaxed/stock
      y_dist = [1.385, 1.394, 1.406, 1.421, 1.444, 1.474, 1.516, 1.534, 1.546, 1.568, 1.579, 1.593, 1.614]
      profile_mod_pos = [1.0] * 4
      profile_mod_neg = [1.0] * 4

    sng_TR = 1.8  # reacceleration stop and go TR
    sng_speed = 15.0 * CV.MPH_TO_MS

    if self.car_data.v_ego > sng_speed:  # keep sng distance until we're above sng speed again
      self.sng = False

    if (self.car_data.v_ego >= sng_speed or self.df_data.v_egos[0]['v_ego'] >= self.car_data.v_ego) and not self.sng:  # if above 15 mph OR we're decelerating to a stop, keep shorter TR. when we reaccelerate, use sng_TR and slowly decrease
      TR = interp(self.car_data.v_ego, x_vel, y_dist)
    else:  # this allows us to get closer to the lead car when stopping, while being able to have smooth stop and go when reaccelerating
      self.sng = True
      x = [sng_speed / 3.0, sng_speed]  # decrease TR between 5 and 15 mph from 1.8s to defined TR above at 15mph while accelerating
      y = [sng_TR, interp(sng_speed, x_vel, y_dist)]
      TR = interp(self.car_data.v_ego, x, y)

    TR_mod = []
    # Dynamic follow modifications (the secret sauce)
    x = [-20.0288, -15.6871, -11.1965, -7.8645, -4.9472, -3.0541, -2.2244, -1.4383, -0.6671, -0.3313, -0.1671, 0.0, 0.6845, 1.3682, 1.898, 2.7316]  # relative velocity values
    y = [0.6492, 0.5155, 0.4235, 0.3357, 0.2491, 0.1278, 0.1092, 0.0841, 0.0509, 0.0209, 0.0051, 0, -0.0443, -0.066, -0.1425, -0.2218]  # modification values
    TR_mod.append(interp(self.lead_data.v_lead - self.car_data.v_ego, x, y))

    # if not self.sng:  # todo: test to see if limitting the accel mod when not in sng is better
    x = [-4.4795, -2.8122, -1.5727, -1.1129, -0.6611, -0.2692, 0.0, 0.1466, 0.5144, 0.6903, 0.9302]  # lead acceleration values
    y = [0.265, 0.1877, 0.0984, 0.0574, 0.034, 0.024, 0.0, -0.009, -0.042, -0.053, -0.059]  # modification values
    TR_mod.append(interp(self._calculate_lead_accel(), x, y))

    profile_mod_pos = interp(self.car_data.v_ego, profile_mod_x, profile_mod_pos)
    profile_mod_neg = interp(self.car_data.v_ego, profile_mod_x, profile_mod_neg)

    # if self.sng:  # only if we're in sng todo: test this
    x = [sng_speed / 5.0, sng_speed]  # as we approach 0, apply x% more distance
    y = [1.1, 1.0]
    profile_mod_pos *= interp(self.car_data.v_ego, x, y)

    TR_mod = sum([mod * profile_mod_neg if mod < 0 else mod * profile_mod_pos for mod in TR_mod])  # alter TR modification according to profile
    TR += TR_mod

    if self.CS.leftBlinker or self.CS.rightBlinker and self.df_profile != self.df_profiles.traffic:
      x = [8.9408, 22.352, 31.2928]  # 20, 50, 70 mph
      y = [1.0, .75, .65]  # reduce TR when changing lanes
      TR *= interp(self.car_data.v_ego, x, y)
    return clip(TR, 0.9, 2.7)

  def update_lead(self, v_lead=None, a_lead=None, x_lead=None, status=False, new_lead=False):
    self.lead_data.v_lead = v_lead
    self.lead_data.a_lead = a_lead
    self.lead_data.x_lead = x_lead
    self.lead_data.status = status
    self.lead_data.new_lead = new_lead

  def update_car(self):
    self.car_data.v_ego = self.CS.vEgo
    self.car_data.a_ego = self.CS.aEgo
