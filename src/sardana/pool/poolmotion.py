#!/usr/bin/env python

##############################################################################
##
## This file is part of Sardana
##
## http://www.tango-controls.org/static/sardana/latest/doc/html/index.html
##
## Copyright 2011 CELLS / ALBA Synchrotron, Bellaterra, Spain
## 
## Sardana is free software: you can redistribute it and/or modify
## it under the terms of the GNU Lesser General Public License as published by
## the Free Software Foundation, either version 3 of the License, or
## (at your option) any later version.
## 
## Sardana is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU Lesser General Public License for more details.
## 
## You should have received a copy of the GNU Lesser General Public License
## along with Sardana.  If not, see <http://www.gnu.org/licenses/>.
##
##############################################################################

"""This module is part of the Python Pool libray. It defines the class for a
motion"""

__all__ = [ "MotionState", "MotionMap", "PoolMotion", "PoolMotionItem" ]

__docformat__ = 'restructuredtext'

import time

from taurus.core.util import Enumeration, DebugIt

from sardana import State
from poolaction import ActionContext, PoolActionItem, PoolAction

#: enumeration representing possible motion states
MotionState = Enumeration("MotionSate", ( \
    "Stopped",
#    "StoppedOnError",
#    "StoppedOnAbort",
    "Moving",
    "MovingBacklash",
    "MovingInstability",
    "Invalid") )

MS = MotionState
MovingStates = MS.Moving, MS.MovingBacklash, MS.MovingInstability
StoppedStates = MS.Stopped, #MS.StoppedOnError, MS.StoppedOnAbort

#MotionAction = Enumeration("MotionAction", ( \
#    "StartMotion",
#    "Finish",
#    "Abort",
#    "NoAction",
#    "Invalid") )

#MA = MotionAction

MotionMap = {
    #MS.Stopped           : State.On,
    MS.Moving            : State.Moving,
    MS.MovingBacklash    : State.Moving,
    MS.MovingInstability : State.Moving,
    MS.Invalid           : State.Invalid,
}


class PoolMotionItem(PoolActionItem):
    """An item involved in the motion. Maps directly to a motor object"""
    
    def __init__(self, moveable, position, dial_position, do_backlash,
                 backlash, instability_time=None):
        PoolActionItem.__init__(self, moveable)
        self.position = position
        self.interrupted = False
        self.dial_position = dial_position
        self.do_backlash = do_backlash
        self.backlash = backlash
        self.instability_time = instability_time
        self.old_motion_state = MS.Invalid
        self.motion_state = MS.Stopped
        self.start_time = None
        self.stop_time = None
        self.stop_final_time = None
        self.old_state_info = State.Invalid, "Uninitialized", (False,False,False)
        self.state_info = State.On, "Uninitialized", (False,False,False)
        
    def has_instability_time(self):
        return self.instability_time is not None

    def in_motion(self):
        return self.motion_state in MovingStates

    def get_moveable(self):
        return self.element
    
    moveable = property(fget=get_moveable)

    def get_state_info(self):
        si = self.state_info
        return MotionMap.get(self.motion_state, si[0]), si[1], si[2]

    def start(self, new_state):
        self.old_state_info = self.state_info
        self.state_info = new_state, self.state_info[1], self.state_info[2]

    def stopped(self, timestamp):
        self.stop_time = timestamp
        if self.instability_time is None:
            self.stop_final_time = timestamp
            new_ms = MS.Stopped
        else:
            new_ms = MS.MovingInstability
        return new_ms
    
    def handle_instability(self, timestamp):
        new_ms = MS.MovingInstability
        dt = timestamp - self.stop_time
        if dt >= self.instability_time:
            self.stop_final_time = timestamp
            new_ms = MS.Stopped
        return new_ms
    
    def on_state_switch(self, state_info, timestamp=None):
        if timestamp is None:
            timestamp = time.time()
        self.old_state_info = self.state_info
        self.state_info = state_info
        old_state = self.old_state_info[0]
        state = state_info[0]
        new_ms = ms = self.motion_state
        moveable = self.moveable
        self.interrupted = moveable.was_interrupted()
        
        if self.interrupted:
            if ms == MS.MovingInstability:
                new_ms = self.handle_instability(timestamp)
            elif state == State.Moving:
                new_ms = MS.Moving
            elif old_state == State.Moving:
                new_ms = self.stopped(timestamp)
        elif ms == MS.Stopped:
            if state == State.Moving:
                self.start_time = timestamp
                new_ms = MS.Moving
        elif ms == MS.Moving:
            if state != State.Moving:
                if self.do_backlash and state == State.On:
                    new_ms = MS.MovingBacklash
                else:
                    new_ms = self.stopped(timestamp)
        elif ms == MS.MovingBacklash:
            if state != State.Moving:
                new_ms = self.stopped(timestamp)
        elif ms == MS.MovingInstability:
            new_ms = self.handle_instability(timestamp)
        self.old_motion_state, self.motion_state = ms, new_ms
        return ms, new_ms
    
    
class PoolMotion(PoolAction):
    """This class manages motion actions"""
    
    def __init__(self, pool, name="GlobalMotion"):
        PoolAction.__init__(self, pool, name)
        self._motion_info = None
        self._motion_sleep_time = None
        self._nb_states_per_position = None
    
    def _recover_start_error(self, ctrl, meth_name, read_state=False):
        self.warning("%s throws exception on %s. Stopping...", ctrl, meth_name)
        self.debug("Details:", exc_info=1)
        try:
            self.stop_action()
        except:
            self.error("Could not stop. Trying an abort!")
            self.debug("Details:", exc_info=1)
            try:
                self.abort_action()
            except:
                self.critical("Could not abort!")
                self.debug("Details:", exc_info=1)
        if read_state:
            states = {}
            self.read_state_info(ret=states)
            for moveable, state_info in states.items():
                state_info = moveable._from_ctrl_state_info(state_info)
                moveable._set_state_info(state_info)
            
    def pre_start_all(self, pool_ctrls):
        # PreStartAll on all controllers
        for pool_ctrl in pool_ctrls:
            try:
                pool_ctrl.ctrl.PreStartAll()
            except:
                self._recover_start_error(pool_ctrl, "PreStartAll")
                raise
    
    def pre_start_one(self, moveables, items):
        # PreStartOne on all elements
        for moveable in moveables:
            pool_ctrl = moveable.controller
            ctrl, axis = pool_ctrl.ctrl, moveable.axis
            dial = items[moveable][1]
            ret = ctrl.PreStartOne(axis, dial)
            if not ret:
                try:
                    msg = "%s.PreStartOne(%s(%d), %f) returns False" \
                           % (pool_ctrl.name, moveable.name, axis, dial)
                    raise Exception(msg)
                except:
                    self._recover_start_error(pool_ctrl, "PreStartOne")
                    raise
    
    def start_one(self, moveables, motion_info):
        # StartOne on all elements
        for moveable in moveables:
            pool_ctrl = moveable.controller
            ctrl = pool_ctrl.ctrl
            axis = moveable.axis
            dial_position = motion_info[moveable].dial_position
            try:
                ctrl.StartOne(axis, dial_position)
            except:
                self._recover_start_error(pool_ctrl, "StartOne")
                raise

    def start_all(self, pool_ctrls, moveables, motion_info):
        # Change the state to Moving
        for moveable in moveables:
            moveable_info = motion_info[moveable]
            moveable.set_state(State.Moving, propagate=2)
            state_info = moveable.inspect_state(), \
                moveable.inspect_status(), \
                moveable.inspect_limit_switches()
            moveable_info.on_state_switch(state_info)
        
        # StartAll on all controllers
        for pool_ctrl in pool_ctrls:
            try:
                pool_ctrl.ctrl.StartAll()
            except:
                self._recover_start_error(pool_ctrl, "StartOne", read_state=True)
                raise

    def start_action(self, *args, **kwargs):
        """items -> dict<moveable, (pos, dial, do_backlash, backlash)"""
        
        items = kwargs.pop("items")
        
        pool = self._pool
        
        # prepare data structures
        self._aborted = False
        self._stopped = False
        self._motion_sleep_time = kwargs.pop("motion_sleep_time",
                                             pool.motion_loop_sleep_time)
        self._nb_states_per_position = \
            kwargs.pop("nb_states_per_position",
                       pool.motion_loop_states_per_position)
        
        self._motion_info = motion_info = {}
        for moveable, motion_data in items.items():
            it = moveable.instability_time
            motion_info[moveable] = PoolMotionItem(moveable, *motion_data,
                                                   instability_time=it)
        
        pool_ctrls = self.get_pool_controller_list()
        moveables = self.get_elements()
        
        with ActionContext(self) as context:
            self.pre_start_all(pool_ctrls)
            self.pre_start_one(moveables, items)
            self.start_one(moveables, motion_info)
            self.start_all(pool_ctrls, moveables, motion_info)
            
    def backlash_item(self, motion_item):
        moveable = motion_item.moveable
        controller = moveable.controller
        axis = moveable.axis
        position = motion_item.backlash
        try:
            controller.move({axis:position})
        except:
            self.warning("could not start backlash on %s", moveable.name,
                         exc_info=1)
    
    @DebugIt()
    def action_loop(self):
        i = 0
        
        states, positions = {}, {}
        for k in self.get_elements():
            states[k] = None
            positions[k] = None
        
        nap = self._motion_sleep_time
        nb_states_per_pos = self._nb_states_per_position
        
        # read positions to send a first event when starting to move
        with ActionContext(self) as context:
            positions = self.raw_read_dial_position()
            for moveable, position_info in positions.items():
                moveable.put_dial_position(position_info, propagate=2)
        
        while True:
            self.read_state_info(ret=states)
            timestamp = time.time()
            in_motion = False
            for moveable, state_info in states.items():
                motion_item = self._motion_info[moveable]
                state_info = moveable._from_ctrl_state_info(state_info)
                
                state, status, limit_switches = state_info
                old_motion_state, motion_state = \
                    motion_item.on_state_switch(state_info, timestamp=timestamp)
                real_state_info = motion_item.get_state_info()
                interrupted = moveable.was_interrupted()
                well_stopped = state == State.On and not interrupted
                moving = motion_item.in_motion()
                
                start_backlash = motion_state == MS.MovingBacklash and \
                    old_motion_state != MS.MovingBacklash
                start_instability = motion_state == MS.MovingInstability and \
                    old_motion_state != MS.MovingInstability
                stopped_now = not moving and old_motion_state in MovingStates

                # make sure the state is placed in the motor
                moveable.put_state_info(real_state_info)
                
                # if motor stopped 'well' and there is a backlash to do...
                if start_backlash:
                    moveable.debug("Starting backlash")
                    # make sure the last position after the first motion is
                    # sent before starting the backlash motion
                    moveable.get_position(cache=False, propagate=2)
                    self.backlash_item(motion_item)
                    moving = motion_item.in_motion()
                elif start_instability:
                    moveable.debug("Starting to wait for instability")
                    # make sure the last position after the first motion is
                    # sent before starting the backlash motion
                    moveable.get_position(cache=False, propagate=2)
                elif stopped_now:
                    moveable.debug("Stopped")
                    
                    # try to read a last position to force an event
                    moveable.get_position(cache=False, propagate=2)
                    
                    ## free the motor from the OperationContext so we can send
                    ## a state event. Otherwise we may be asked to move the motor
                    ## again which would result in an exception saying that the
                    ## motor is already involved in an operation
                    ## ... but before protect the motor so that the monitor
                    ## doesn't come in between the two instructions below and
                    ## send a state event on it's own
                    with moveable:
                        moveable.clear_operation()
                        moveable.set_state_info(real_state_info, propagate=2)
                    
                # Then update the state
                if not stopped_now:
                    moveable.set_state_info(real_state_info, propagate=1)
                
                if moving:
                    in_motion = True
                
            if not in_motion:
                break
            
            # read position every n times
            if not i % nb_states_per_pos:
                _start = time.time()
                self.read_dial_position(ret=positions)
                for moveable, position_info in positions.items():
                    moveable.put_dial_position(position_info)
            
            i += 1
            time.sleep(nap)
    
    def read_dial_position(self, ret=None, serial=False):
        return self.read_value(ret=ret, serial=serial)

    def raw_read_dial_position(self, ret=None, serial=False):
        return self.raw_read_value(ret=ret, serial=serial)
    
