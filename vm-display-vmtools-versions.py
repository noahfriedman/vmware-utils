#!/usr/bin/env python

from   __future__ import print_function

import os
import sys
import time

sys.path.insert( 0, os.getenv( 'HOME' ) + '/bin/vmware' )

from   pyVmomi    import vim
import vspherelib     as vsl

toolsTypeMap = { 'tar'         : 'vSphere',
                 'msi'         : 'vSphere',
                 'openvmtools' : 'open-vm-tools', }

def get_args():
    p = vsl.ArgumentParser( loadrc=True )
    return p.parse()


def main():
    args = get_args()
    vsi = vsl.vmomiConnect( args )

    props = ( 'name',
              'config.extraConfig',  # array; expensive to fetch
              'config.guestId',
              'config.template',
              'config.tools.toolsInstallType',
              'config.tools.toolsUpgradePolicy',
              'config.tools.toolsVersion',
              'runtime.powerState',
            )
    vmlist = vsi.get_obj_props( [vim.VirtualMachine], props )

    for vm in vmlist:
        del vm['obj']

        vm[ 'state' ] = ' '
        if vm[ 'config.template' ]:
            vm[ 'state' ] = '+'
            del vm[ 'config.template' ]
        elif vm ['runtime.powerState'] == 'poweredOff':
            vm[ 'state' ] = 'o'
            del vm[ 'runtime.powerState' ]

        prop = 'config.guestId'
        if prop in vm: vm[ prop ] = vm[ prop ][ 0 : -5 ]

        prop = 'config.extraConfig'
        it = 'internalversion'
        vm[ it ] = ''
        if prop in vm:
            ec = vm[ prop ]
            del  vm[ prop ]
            ver = vsl.attr_get( ec, 'vmware.tools.internalversion' )
            if ver: vm[ it ] = ver

        prop = 'config.tools.toolsUpgradePolicy'
        if prop in vm:
            vm[ prop ] = vm[ prop ].replace( 'upgradeAtPowerCycle', 'poweron' )

        prop = 'config.tools.toolsInstallType'
        if prop in vm and len( vm[ prop ] ) > 14:
            key = vm[ prop ][ 14: ].lower()
            vm[ prop ] = toolsTypeMap.get( key, key )

    namewidth = max( len( vm['name'] ) for vm in vmlist )
    fmt = '{4} {1:<{0}}  {2:<18}  {3:>10}  {5:<7}  {6}'

    vmlist.sort( key=lambda x: x['name'] )
    for vm in vmlist:

        print( fmt.format( namewidth,
                           vm['name'],
                           vm['config.guestId'],
                           vm[ 'internalversion' ],
                           vm[ 'state' ],
                           vm.get('config.tools.toolsUpgradePolicy', ''),
                           vm.get('config.tools.toolsInstallType', ''),
                        ) )

if __name__ == '__main__':
    main()
