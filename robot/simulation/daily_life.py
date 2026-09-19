"""Deterministic resident animation, isolated from camera inference and policy.

Only mocap bodies move. Robot qpos, controls and sensor classifications are
never authored here. Walking uses metre-scaled routes and a 0.4 m/s gait;
shortened household dwell times are explicitly a demo schedule.
"""
from __future__ import annotations
import math
import numpy as np
from robot.simulation.scenes import element, geom

PARTS = ('torso','head','pelvis','left_thigh','left_shin','left_shoe',
         'right_thigh','right_shin','right_shoe','left_upper_arm','left_forearm',
         'right_upper_arm','right_forearm')
SKIN = (.72,.51,.38,1)
SHIRT = (.48,.23,.31,1)
PANTS = (.23,.29,.36,1)


def add_resident(world):
    for part in PARTS:
        body = element(world,'body',name='env_resident_'+part,mocap='true',pos=(0,0,1))
        color = SKIN if part in ('head',) or part.endswith('forearm') else PANTS if any(s in part for s in ('thigh','shin','pelvis')) else SHIRT
        size = (.23,.14,.3) if part=='torso' else (.145,.13,.18) if part=='head' else (.17,.13,.13) if part=='pelvis' else (.065,.13,.065) if part.endswith('shoe') else (.065,.065,.20)
        geom(body,'resident_'+part,'ellipsoid',(0,0,0),size,color,contype=0,conaffinity=0)
        if part=='head':
            geom(body,'resident_hair','ellipsoid',(0,.015,.075),(.15,.13,.12),(.77,.77,.74,1),contype=0,conaffinity=0)
            for side in (-1,1):
                geom(body,f'resident_lens_{side}','ellipsoid',(side*.064,-.121,.012),(.052,.015,.032),(.11,.14,.15,1),contype=0,conaffinity=0)
            geom(body,'resident_nose','ellipsoid',(0,-.14,-.022),(.03,.032,.04),SKIN,contype=0,conaffinity=0)


def _quaternion(matrix):
    # Robust matrix-to-quaternion, including the 180 degree limb poses.
    import mujoco
    result=np.zeros(4)
    mujoco.mju_mat2Quat(result,np.asarray(matrix,dtype=float).reshape(9))
    return result


def _segment(a,b):
    delta=b-a
    z=delta/np.linalg.norm(delta)
    ref=np.array([0.,1.,0.]) if abs(z[1])<.9 else np.array([1.,0.,0.])
    x=np.cross(ref,z);x/=np.linalg.norm(x)
    return (a+b)/2,np.column_stack((x,np.cross(z,x),z))


class ResidentRoutine:
    def __init__(self,model,data,scene):
        self.model,self.data=model,data
        self.ids={part:int(model.body('env_resident_'+part).mocapid[0]) for part in PARTS}
        # Door centres and furniture approach points avoid authored walls.
        self.stages=[
            ('Reading','sitting',12,[(-3,-3.65)]),
            ('Walking to breakfast','walking',None,[(-3,-3.65),(-2,-4),(0,-4),(1.7,-4),(2.9,-4.35)]),
            ('Having breakfast','standing',10,[(2.9,-4.35)]),
            ('Walking to bedroom','walking',None,[(2.9,-4.35),(1.7,-4),(0,-4),(0,4),(-2.2,4),(-3.8,3),(-5,3)]),
            ('Resting in bed','lying_bed',12,[(-5,3)]),
            ('Walking to living room','walking',None,[(-5,3),(-3.8,3),(-2.2,4),(0,4),(0,-4),(-2,-4),(-3,-3.65)]),
        ]
        self.durations=[duration if duration is not None else sum(math.dist(a,b) for a,b in zip(points,points[1:]))/.4 for _,_,duration,points in self.stages]
        self.duration=sum(self.durations)
        self.reset()

    def reset(self):
        self.offset=0.;self.interrupt=None;self.now=0.;self.position=np.array([-3.,-3.65]);self.yaw=0.;self._state={}
        self.update(0.)

    def trigger(self,kind):
        if kind=='fall':
            self.interrupt={'start':self.now,'position':self.position.copy(),'yaw':self.yaw,'routine_time':self.now-self.offset}
        elif kind in ('routine','recover'):
            # Recover stands at the current location, then returns along the
            # same authored route at real walking speed (no actor teleport).
            if self.interrupt:
                self.interrupt={**self.interrupt,'start':self.now,'recover':True}
        else:
            raise ValueError('unknown resident action')
        self.update(self.now)

    def update(self,sim_time):
        self.now=float(sim_time)
        t=(self.now-self.offset)%self.duration
        phase_start=0.
        for index,((activity,posture,_,points),duration) in enumerate(zip(self.stages,self.durations)):
            if t<=duration:
                break
            t-=duration;phase_start+=duration
        pos=np.array(points[0],dtype=float);yaw=self.yaw
        if posture=='walking':
            travel=t*.4
            for a,b in zip(points,points[1:]):
                length=math.dist(a,b)
                if travel<=length:
                    fraction=travel/length
                    pos=np.array(a)+(np.array(b)-a)*fraction
                    # Person faces local -Y.
                    yaw=math.atan2(b[1]-a[1],b[0]-a[0])+math.pi/2
                    break
                travel-=length
        elif posture=='standing':
            yaw=math.pi
        else:
            yaw=0.
        fall=0.;base_z=0.
        if posture=='lying_bed':
            # Smoothly lower onto the bed and stand back up at its edge.
            blend=min(1.,t/1.5,(duration-t)/1.5)
            fall=max(0.,blend)*math.pi/2
            base_z=.73*max(0.,blend)
        if self.interrupt:
            interrupt=self.interrupt
            pos=interrupt['position'];yaw=interrupt['yaw']
            elapsed=max(0.,self.now-interrupt['start'])
            if interrupt.get('recover'):
                if elapsed>=1.5:
                    self.offset=self.now-interrupt['routine_time']
                    self.interrupt=None
                    return self.update(self.now)
                fall=max(0.,1-elapsed/1.5)*math.pi/2
                posture='standing' if elapsed>=1.5 else 'recovering'
                activity='Standing after recovery' if elapsed>=1.5 else 'Getting up'
            else:
                fall=min(1.,elapsed/1.5)*math.pi/2
                posture='lying_floor' if elapsed>=1.5 else 'falling'
                activity='Staged fall — waiting for Annie'
            base_z=.14*math.sin(fall)
        self.position=pos.copy();self.yaw=yaw
        self._pose(pos,yaw,posture,t,fall,base_z)
        self._state={'activity':activity,'posture':posture,'phase':activity,'position_m':[float(pos[0]),float(pos[1])],
                     'progress':min(1.,t/duration),'speed_mps':.4 if posture=='walking' else 0.,
                     'cycle_duration_s':self.duration,'source':'authored_mocap_animation',
                     'timing':'real walking speed; shortened demo dwell times','trigger_active':bool(self.interrupt)}

    def _pose(self,pos,yaw,posture,t,fall,base_z):
        sitting=posture=='sitting';walk=posture=='walking'
        hip=.60 if sitting else .87
        skeleton={'torso':(np.array([0.,0,hip+.26]),np.eye(3)),
                  'pelvis':(np.array([0.,0,hip]),np.eye(3)),
                  'head':(np.array([0.,0,hip+.71]),np.eye(3))}
        swing=.18*math.sin(t*2*math.pi*1.05) if walk else 0.
        for name,side in (('left',-1),('right',1)):
            phase=swing*side
            knee=np.array([side*.12,-.37 if sitting else phase*.45,.49])
            ankle=np.array([side*.12,-.40 if sitting else phase,.105+(.035*max(0.,math.sin(t*6.6+side*math.pi/2)) if walk else 0)])
            skeleton[name+'_thigh']=_segment(np.array([side*.12,0.,hip]),knee)
            skeleton[name+'_shin']=_segment(knee,ankle)
            skeleton[name+'_shoe']=(ankle+np.array([0.,-.05,-.04]),np.eye(3))
            shoulder=np.array([side*.25,0.,hip+.43]);elbow=np.array([side*.28,-phase*.4,hip+.14])
            wrist=np.array([side*.23,-.25 if sitting else -phase*.7,hip-.09])
            skeleton[name+'_upper_arm']=_segment(shoulder,elbow)
            skeleton[name+'_forearm']=_segment(elbow,wrist)
        c,s=math.cos(yaw),math.sin(yaw);cf,sf=math.cos(fall),math.sin(fall)
        rotation=np.array([[c,-s,0],[s,c,0],[0,0,1]])@np.array([[1,0,0],[0,cf,sf],[0,-sf,cf]])
        for part,(point,local_rotation) in skeleton.items():
            ident=self.ids[part]
            self.data.mocap_pos[ident]=rotation@point+np.array([pos[0],pos[1],base_z])
            self.data.mocap_quat[ident]=_quaternion(rotation@local_rotation)

    def state(self):
        return dict(self._state)
