# vspherelib.py --- convenience functions for vsphere client applications

# Author: Noah Friedman <friedman@splode.com>
# Created: 2017-10-31
# Public domain

# $Id: vspherelib.py,v 1.30 2018/08/05 08:34:28 friedman Exp $

# Commentary:
# Code:

from __future__ import print_function

import argparse
import getpass
import os
import sys
import OpenSSL
import ssl
import re
import time
import atexit

from pyVim      import connect as pyVconnect
from pyVmomi    import vim, vmodl

# Get the id for a managed object type: Folder, Datacenter, Datastore, etc.
vim.ManagedObject.id = property( lambda self: self._moId )

try:
    progname = os.path.basename( sys.argv[0] )
except:
    pass


debug = bool( os.getenv( 'VSPHERELIB_DEBUG' ))

# Class decorator to avoid stacktraces on uncaught exceptions
# in the decorated class unless debugging is enabled.
def conditional_stacktrace( wrapped_class ):
    def conditional_stacktrace_excepthook( extype, val, bt ):
        sys.excepthook = sys.__excepthook__
        if issubclass( extype, wrapped_class ):
            print( ': '.join( ( os.path.basename( sys.argv[0] ),
                                str( val )) ),
                   file=sys.stderr )
        else:
            sys.__excepthook__( extype, val, bt )

    class conditional_exception( wrapped_class ):
        def __init__( self, reason ):
            self.reason = str( reason )
            if not debug and sys.excepthook is sys.__excepthook__:
                sys.excepthook = conditional_stacktrace_excepthook

        def __str__( self ):
            return self.reason

    return conditional_exception

@conditional_stacktrace
class vmomiError( Exception ): pass
class NameNotFoundError(     vmomiError ): pass
class NameNotUniqueError(    vmomiError ): pass
class ConnectionFailedError( vmomiError ): pass
class RequiredArgumentError( vmomiError ): pass


class Diag( object ):
    def __init__( self, *args, **kwargs ):
        self.sep    = kwargs.get( 'sep',  ': ' )
        self.lines  = []
        self.append( *args )

    def __str__( self ):
        if not self.lines:
            return ""
        return "\n".join( self.lines )

    def append( self, *args ):
        if args:
            self.lines.append( self.sep.join( args ))


class Timer( object ):
    enabled  = bool( os.getenv( "VSPHERELIB_TIMER" ))
    acc_tm   = 0
    acc_cl   = 0
    fmt      = '{0:<40}: {1: > 8.4f}s / {2:> 8.4f}s'
    fh       = sys.stderr

    def __init__( self, label ):
        self.label = label
        self.beg_cl = time.clock()
        self.beg_tm = time.time()

    def report( self ):
        if not self.enabled: return
        tot_tm = time.time()  - self.beg_tm
        tot_cl = time.clock() - self.beg_cl
        self.__class__.acc_tm += tot_tm
        self.__class__.acc_cl += tot_cl

        # Allows for delayed evaluation
        if callable( self.label ):
            self.label = self.label()

        print( self.fmt.format( self.label, tot_cl, tot_tm ),
               file=self.fh )

    def _atexit_report_accum():
        if not Timer.enabled: return
        print( Timer.fmt.format( "TOTAL", Timer.acc_cl, Timer.acc_tm ),
               file=Timer.fh )
    atexit.register( _atexit_report_accum )

# end class Timer


class ArgumentParser( argparse.ArgumentParser, object ):
    class Option(): pass  # just a container

    # alias
    add        = argparse.ArgumentParser.add_argument

    searchpath = ['XDG_CONFIG_HOME', 'HOME']
    rcname     = '.vspherelibrc.py'

    def __init__( self, rest=None, help='Remaining arguments', required=False, **kwargs ):
        super( self.__class__, self ).__init__( **kwargs )

        timer = Timer( 'loadrc' )
        opt = self.opt = self.loadrc()
        timer.report()

        nargs = '*'
        if required:
            if type( required ) is bool:
                nargs = '+'
            else:
                nargs = required
            if not rest:
                rest = 'rest'

        self.add( '-s', '--host',     default=opt.host,           help='Remote esxi/vcenter host to connect to' )
        self.add( '-o', '--port',     default=opt.port, type=int, help='Port to connect on' )
        self.add( '-u', '--user',     default=opt.user,           help='User name for host connection' )
        self.add( '-p', '--password', default=opt.password,       help='Server user password' )
        if rest:
            self.add( rest, nargs=nargs,                          help=help )

    # The rc file can manipulate this 'opt' variable; for example it could
    # provide a default for the host via:
    # 	opt.host = 'vcenter1.mydomain.com'
    def loadrc( self ):
        opt          = self.Option()
        opt.host     = None
        opt.port     = 443
        opt.user     = os.getenv( 'LOGNAME' )
        opt.password = None

        env_rc = os.getenv( 'VSPHERELIBRC' )
        if env_rc:
            try:
                execfile( env_rc )
            except IOError:
                pass
            return opt
        else:
            for env in self.searchpath:
                if env in os.environ:
                    try:
                        execfile( os.environ[env] + "/" + self.rcname )
                    except IOError:
                        continue
                    return opt
        return opt

    def parse_args( self ):
        args = super( self.__class__, self ).parse_args()

        if not args.host:
            raise RequiredArgumentError( 'Server host is required' )

        if args.password:
            pass
        elif os.getenv( 'VMPASSWD' ):
            args.password = os.getenv( 'VMPASSWD' )
        else:
            prompt = 'Enter password for %(user)s@%(host)s: ' % vars( args )
            args.password = getpass.getpass( prompt )

        extra = vars( self.opt )
        for elt in extra:
            if elt.find( '_' ) != 0 and not hasattr( args, elt ):
                setattr( args, elt, extra[ elt ] )

        return args
    # alias
    parse = parse_args


# end class ArgumentParser


######
## Class for handling property list parameters in a more systematic way.
######

class propList( object ):
    def __new__( self, *args ):
        """If first param is already an instance, just return previous instance"""
        if type( args[0] ) is propList:
            return args[0]
        else:
            return super( self.__class__, self ).__new__( propList, *args )

    def __init__( self, *args ):
        if type( args[0] ) is propList:
            return
        self.proplist = []
        self.propdict = {}
        self.add_if_new( *args )

    def __len__( self ):
        return len( self.proplist )

    def add_if_new( self, *props ):
        for elt in props:
            if type( elt ) is dict:
                self.propdict.update( elt )
                self.add_if_new( *elt.keys() )
            elif type( elt ) in (tuple, list):
                self.add_if_new( *elt )
            elif elt not in self.proplist:
                self.proplist.append( elt )

    def names( self ):
        return list( self.proplist )

    def filters( self ):
        return dict( self.propdict )

# end class propList


######
## Mixins for collecting managed objects and properties
######

class _vmomiCollect( object ):
    def create_filter_spec( self, vimtype, container, props ):
        props      = propList( props )

        vpc        = vmodl.query.PropertyCollector
        travSpec   = vpc.TraversalSpec( name = 'traverseEntities',
                                        path = 'view',
                                        skip = False,
                                        type = type( container ) )
        objSpec    = vpc.ObjectSpec( obj=container, skip=True, selectSet=[ travSpec ] )
        propSet    = [ vpc.PropertySpec( type    = vimt,
                                         pathSet = props.names(),
                                         all     = not len( props ) )
                       for vimt in vimtype ]
        return vpc.FilterSpec( objectSet=[ objSpec ], propSet=propSet )


    def create_container_view( self, vimtype, root=None, recursive=True ):
        if root is None:
            root = self.si.content.rootFolder
        return self.si.content.viewManager.CreateContainerView(
            container = root,
            type      = vimtype,
            recursive = recursive )


    def create_list_view( self, objs ):
        if type( objs ) is not vim.ManagedObject.Array:
            try:
                objs = map( lambda o: o.obj, objs )
            except AttributeError:
                pass
        return self.si.content.viewManager.CreateListView( obj=objs )


    def _get_obj_props_nofilter( self, vimtype, props=None, root=None, recursive=True ):
        """
        Retrieve all listed properties from objects in container (root), or
        create container out of the rootFolder.

        Returns an object of type vmodl.query.PropertyCollector.ObjectContent[],
        or vim.ManagedObject[] if there are no properties to collect.
        """

        gc_container = False
        if isinstance( root, ( vim.view.ListView, vim.view.ContainerView )):
            container = root
        elif type( root ) is list:
            container    = self.create_list_view( root )
            gc_container = True
        else:
            container    = self.create_container_view( vimtype, root, recursive )
            gc_container = True

        if props is None:
            result = container.view
        else:
            props      = propList( props )
            spc        = self.si.content.propertyCollector
            filterSpec = self.create_filter_spec( vimtype, container, props )

            timer  = Timer( lambda: 'retrieve {} '.format( ', '.join( v._wsdlName for v in vimtype )))
            result = spc.RetrieveContents( [ filterSpec ] )
            timer.report()

        if gc_container:
            container.Destroy()
        return result


    def get_obj_props( self, vimtype, props=None, root=None, recursive=True ):
        """
        If any of the properties have matching values to search for, narrow down
        the view of managed objects to retrieve the full list of attributes from.

        So for example, if you want a laundry list of attributes from a
        VirtualMachine but only the one that has a specific name, it's way
        faster to create a new container with just that machine in it
        before then collecting a dozen properties from it.

        Returns a list of dict objects for each result.

        """
        if props is None or len( props ) < 1:
            return self._get_obj_props_nofilter( vimtype, props, root, recursive )

        # First get the subset of managed objects we want
        props   = propList( props )
        filters = props.filters()
        if filters:
            filterprops = filters.keys()
            res = self._get_obj_props_nofilter( vimtype, filterprops, root, recursive )
            match = []
            for r in res:
                for rprop in r.propSet:
                    if rprop.val in filters.get( rprop.name ):
                        match.append( r )
                        break
            if not match:
                # there were filters but nothing matched.  So don't let the
                # collector below run since it would collect *everything*
                return
        else:
            # No filters, so fetch everything in the container
            match = root

        # Now get all the props from the selected objects... if there are
        # any we don't already have.  (propnames is a superset of filters)
        propnames = props.names()
        if len( filters ) != len( propnames ):
            res = self._get_obj_props_nofilter( vimtype, propnames, match, recursive )
        else:
            res = match

        if res and len( res ) > 0:
            result = []
            for r in res:
                elt = propset_to_dict( r.propSet )
                elt[ 'obj' ] = r.obj
                result.append( elt )
            return result


    def get_obj( self, *args, **kwargs):
        result = self.get_obj_props( *args, **kwargs )
        if not result:
            return
        if kwargs.get( 'props' ) or len( args ) > 1:
            return [ elt[ 'obj' ] for elt in result ]
        else:
            return result

# end class _vmomiCollect


######
## mixins for retrieving Managed Objects by name
######

class _vmomiFind( object ):
    def _get_single( self, name, mot, label, root=None ):
        """If name is null but there is only one object of that type anyway, just return that."""
        def err( exception, msg, res=root ):
            if not isinstance( res, vim.ManagedObject.Array):
                res = self.get_obj( mot, root=res )
            diag = Diag( msg )
            if not res:
                raise exception( diag )
            diag.append( 'Available {0}s:'.format( label ) )
            for n in sorted( [elt.name for elt in res] ):
                diag.append( "\t" + n )
            raise exception( diag )

        if name:
            if type( root ) is vim.ManagedObject.Array:
                found = filter( lambda o: o.name == name, root )
            else:
                found = self.get_obj( mot, { 'name' : name }, root=root )

            if not found:
                 err( NameNotFoundError, '{}: {} not found or not available.'.format( name, label ) )
            elif len( found ) > 1:
                err( NameNotUniqueError, '{}: name is not unique.'.format( name ), found )
        else:
            if type( root ) is vim.ManagedObject.Array:
                found = root
            else:
                found = self.get_obj( mot, root=root )

            if not found:
                raise NameNotFoundError( 'No {0}s found!'.format( label ))
            elif len( found ) > 1:
                err( NameNotUniqueError,
                     'More than one {0}s exists; specify {0}s to use.'.format( label, label ),
                     found )
        return found[0]

    def get_datacenter( self, name, root=None ):
        return self._get_single( name, [vim.Datacenter], 'datacenter', root=root )

    def get_cluster( self, name, root=None ):
        return self._get_single( name, [vim.ComputeResource], 'cluster', root=root )

    def get_datastore( self, name, root=None ):
        return self._get_single( name, [vim.Datastore], 'datastore', root=root )

    def get_pool( self, name, root=None ):
        return self._get_single( name, [vim.ResourcePool], 'resource pool', root=root )

    def get_network( self, name, root=None ):
        return self._get_single( name, [vim.Network], 'network label', root=root )

    # These are a subset of vim.Network
    def get_portgroup( self, name, root=None ):
        return self._get_single( name, [vim.dvs.DistributedVirtualPortgroup], 'portgroup', root=root )

    def get_vm( self, name, root=None ):
        return self._get_single( name, [vim.VirtualMachine], 'virtual machine', root=root )

    # TODO: for hosts which still can't be found from the searchindex,
    # try a substring match on all host names.
    def find_vm( self, *names, **kwargs ):
        args = None # make copy of names since we alter
        if type( names[0] ) is not str:
            args = list( names[0] )
        else:
            args = list( names )
        sortord = list( args )
        found = []

        vmlist = self.get_obj_props( [vim.VirtualMachine], { 'name' : args } )
        if vmlist:
            for vm in vmlist:
                found.append( vm[ 'obj' ] )
                try:
                    args.remove( vm[ 'name' ] )
                except ValueError:
                    printerr( vm )

        if len( args ) > 0:
            search = self.si.content.searchIndex
            for vmname in args:
                i = sortord.index( vmname )

                res = search.FindByDnsName( vmSearch=True, dnsName=vmname )
                if res:
                    found.append( res )
                    sortord.pop( i )
                    sortord.insert( i, res.name )
                else:
                    res = search.FindByIp( vmSearch=True, ip=vmname )
                    if res:
                        found.append( res )
                        sortord.pop( i )
                        sortord.insert( i, res.name )

        if kwargs.get( 'showerrors', True ) and len( found ) < len( sortord ):
            found_names = map( lambda o: o.name, found )
            for name in sortord:
                if name not in found_names:
                    printerr( '"{}"'.format( name ), 'virtual machine not found.' )

        self.vmlist_sort_by_args( found, sortord )
        return found

    def vmlist_sort_by_args( self, vmlist, args ):
        if not vmlist or len( vmlist ) < 1:
            return
        vmorder = dict( (elt[ 1 ], elt[ 0 ]) for elt in enumerate( args ) )
        cmp_fn = lambda a, b: cmp( vmorder[ a.name ], vmorder[ b.name ] )
        if type( vmlist[ 0 ] ) is vmodl.query.PropertyCollector.ObjectContent:
            cmp_fn = lambda a, b: cmp( vmorder[ a.obj.name ], vmorder[ b.obj.name ] )
        vmlist.sort( cmp=cmp_fn )

# end class _vmomiFinder


######
## folder-related mixins
######

class _vmomiFolderMap( object ):
    def _init_path_folder_maps( self ):
        p2f = self.path_to_folder_map()
        f2p = { p2f[k] : k for k in p2f }
        self._path_to_folder_map = p2f
        self._folder_to_path_map = f2p

    # Generate a complete map of full paths to corresponding vsphere folder objects
    def path_to_folder_map( self ):
        mtbl = {}
        for elt in self.get_obj_props( [vim.Folder, vim.Datacenter], ['name', 'parent'] ):
            moId = repr( elt[ 'obj' ] )
            mtbl[ moId ] = [ repr( elt[ 'parent' ] ), elt[ 'name' ], elt ]

        rootFolder  = self.si.content.rootFolder
        vmFolderH   = { repr( elt.vmFolder ) : elt.vmFolder for elt in rootFolder.childEntity }

        ptbl = {}
        for moId in mtbl.keys():
            name = []
            mobj = mtbl[ moId ][ 2 ][ 'obj' ]
            while mtbl.has_key( moId ):
                if vmFolderH.has_key( moId ):
                    par = mtbl[ moId ][0]
                    name.insert( 0, mtbl[ par ][ 1 ])
                    break

                elt   = mtbl[ moId ]
                moId  = elt[ 0 ]
                name.insert( 0, elt[ 1 ] )

            if len(name) > 0:
                name.insert( 0, '' )
                name = str.join('/', name )
                ptbl[ name ] = mobj

        return ptbl

    # Return the path name of the folder object
    def folder_to_path( self, folder ):
        try:
            return self._folder_to_path_map[ folder ]
        except KeyError:
            return None
        except AttributeError:
            self._init_path_folder_maps()
            return self._folder_to_path_map[ folder ]

    # Return the folder object located at path
    def path_to_folder( self, path ):
        try:
            return self._path_to_folder_map[ path ]
        except KeyError:
            return None
        except AttributeError:
            self._init_path_folder_maps()
            return self._path_to_folder_map[ path ]

    def get_vm_folder_path( self, vm ):
        return self.folder_to_path( vm.parent )

# end class _vmomiFolderMap


######
## Network mixins
## resolves network labels and distributed port groups
######

class _vmomiNetworkMap( object ):
    def _get_network_moId_label_map( self ):
        return dict( ( x[ 'obj' ]._moId, x[ 'name' ] ) for x in
                     self.get_obj_props( [vim.Network], [ 'name' ] ) )

    def get_nic_network_label( self, nic ):
        try:
            return nic.backing.deviceName
        except AttributeError:
            pass

        try:
            groupKey = nic.backing.port.portgroupKey
        except AttributeError:
            return
        try:
            return self._network_moId_label_map.get( groupKey, groupKey )
        except AttributeError:
            self._network_moId_label_map = self._get_network_moId_label_map()
            return self._network_moId_label_map.get( groupKey, groupKey )

# end of class _vmomiNetworkMap


######
## GuestInfo subtype mixins
######

class _vmomiGuestInfo( object ):
    def vmguest_dns_config( self, vm ):
        dns = []
        for ipStack in vm.guest.ipStack:
            dconf = ipStack.dnsConfig
            if not dconf:
                continue
            dns.append( {
                'server' : list( dconf.ipAddress ),
                'search' : list( dconf.searchDomain ), })
        return dns

    def vmguest_ip_routes( self, vm ):
        tbl = {}
        for ipStack in vm.guest.ipStack:
            routes = ipStack.ipRouteConfig.ipRoute
            for elt in routes:
                if ( elt.prefixLength == 128
                     or elt.network in ('ff00::', '169.254.0.0')
                     or elt.network.find( 'fe80::' ) == 0 ):
                    continue

                if elt.prefixLength != 0:
                    net = '{}/{}'.format( elt.network, elt.prefixLength )
                elif elt.network == '0.0.0.0':
                    net = 'default'
                else:
                    net = elt.network
                new = { 'network' : net }

                gw = elt.gateway.ipAddress
                if gw:
                    new[ 'gateway' ] = gw

                dev = elt.gateway.device
                if dev not in tbl:
                    eth = tbl[ dev ] = []
                else:
                    eth = tbl[ dev ]
                eth.append( new )
        return list( tbl[i] for i in sorted( tbl.keys() ))

    def vmguest_ip_addrs( self, vm ):
        return [ self.vmnic_cidrs( vmnic )
                 for vmnic in vm.guest.net ]

    # a vmnic is any vim.vm.device.VirtualEthernetCard type element
    # from vm.config.hardware.device
    def vmnic_cidrs( self, vmnic ):
        """
        vmnic should be an object of type vim.vm.GuestInfo.NicInfo


        a vmnic is any vim.vm.device.VirtualEthernetCard type element from
        vm.config.hardware.device
        """
        if vmnic.ipConfig:
            return [ '{}/{}'.format( ip.ipAddress, ip.prefixLength )
                     for ip in vmnic.ipConfig.ipAddress ]
        else:
            return vmnic.ipAddress

    def vmguest_nic_info( self, vm ):
        ethernet = vim.vm.device.VirtualEthernetCard
        nics = []
        for nic in get_seq_type( vm.config.hardware.device, ethernet ):
            prop = { 'obj'        : nic,
                     'type'       : nic._wsdlName.replace( 'Virtual', '' ).lower(),
                     'label'      : nic.deviceInfo.label,
                     'netlabel'   : self.get_nic_network_label( nic ),
                     'macAddress' : nic.macAddress, }
            if vm.summary.runtime.powerState == "poweredOn":
                gnic = filter( lambda g: g.macAddress == nic.macAddress, vm.guest.net )
                if gnic:
                    prop[ 'ip' ] = self.vmnic_cidrs( gnic[0] )
            nics.append( prop )
        return nics



######
## Task-related mixins
######

class _vmomiTask( object ):
    def taskwait( self, tasklist, printsucc=True, callback=None ):
        class our(): pass
        our.callback = callback

        def diag_callback( *args ):
            print( args, file=sys.stderr )
            # Perhaps we can do something more useful here depending on the
            # type of error.  For now, just stop further callbacks
            our.callback = None

        spc = self.si.content.propertyCollector
        vpc = vmodl.query.PropertyCollector

        try:
            isiterable = iter( tasklist )
        except TypeError:
            tasklist = [ tasklist ]

        objSpecs   = [ vpc.ObjectSpec( obj=task ) for task in tasklist ]
        propSpec   = vpc.PropertySpec( type=vim.Task, pathSet=[], all=True )
        filterSpec = vpc.FilterSpec( objectSet=objSpecs, propSet=[ propSpec ] )
        filter     = spc.CreateFilter( filterSpec, True )

        succ     = 1
        taskleft = [ task.info.key for task in tasklist ]
        try:
            version, state = None, None

            while len( taskleft ):
                update  = spc.WaitForUpdates( version )
                version = update.version

                for filterSet in update.filterSet:
                    for objSet in filterSet.objectSet:
                        info = objSet.obj.info

                        for change in objSet.changeSet:
                            if our.callback:
                                try:
                                    our.callback( change, objSet, filterSet, update )
                                except Exception as err:
                                    printerr( 'Callback error', err )
                                    diag_callback( change, objSet, filterSet, update )

                            if change.name == 'info':
                                state = change.val.state
                            elif change.name == 'info.state':
                                state = change.val
                            else:
                                continue

                            if state == vim.TaskInfo.State.success:
                                taskleft.remove( info.key )
                                if printsucc:
                                    print( info.entityName, 'Success', sep=': ' )
                            elif state == vim.TaskInfo.State.error:
                                taskleft.remove( info.key )
                                succ = 0
                                if not our.callback:
                                    printerr( info.entityName, info.error.msg )
        finally:
            if filter:
                filter.Destroy()
        return succ

# end class _vmomiTask


class vmomiConnect( _vmomiCollect,
                    _vmomiFind,
                    _vmomiFolderMap,
                    _vmomiNetworkMap,
                    _vmomiGuestInfo,
                    _vmomiTask, ):

    def __init__( self, *args, **kwargs ):
        kwargs = dict( **kwargs ) # copy; destructively modified
        for arg in args:
            if isinstance( arg, argparse.Namespace ):
                kwargs.update( vars( arg ))

        self.host = kwargs[ 'host' ]
        self.user = kwargs[ 'user' ]
        self.pwd  = kwargs[ 'password' ]
        self.port = int( kwargs.get( 'port', 443 ))
        self.connect()

    def __del__( self ):
        try:
            pyVconnect.Disconnect( self.si )
        except AttributeError:
            pass

    def connect( self ):
        timer = Timer( 'vmomiConnect.connect' )
        try:
            context = None
            if hasattr( ssl, '_create_unverified_context' ):
                context = ssl._create_unverified_context()

            self.si = pyVconnect.SmartConnect( host = self.host,
                                               user = self.user,
                                               pwd  = self.pwd,
                                               port = self.port,
                                               sslContext = context )
        except Exception as e:
            msg = ': '.join(( self.host,
                              'Could not connect',
                              getattr( e, 'msg', str( e ) ) ))
            raise ConnectionFailedError( msg )
        timer.report()

    def mks( self, *args, **kwargs ):
        return vmomiMKS( self, *args, **kwargs )


vmomiConnector = vmomiConnect  # deprecated alias

# end class vmomiConnect


class vmomiMKS( object ):
    def __init__( self, vsi, *args, **kwargs ):
        kwargs = dict( **kwargs ) # copy; destructively modified
        for arg in args:
            if isinstance( arg, argparse.Namespace ):
                kwargs.update( vars( arg ))

        self.host = vsi.host
        self.port = int( vsi.port )

        vc_cert   = ssl.get_server_certificate( (self.host, self.port) )
        vc_pem    = OpenSSL.crypto.load_certificate( OpenSSL.crypto.FILETYPE_PEM, vc_cert )
        content   = vsi.si.content

        self.fingerprint = vc_pem.digest( 'sha1' )
        self.serverGUID  = content.about.instanceUuid
        self.session     = content.sessionManager.AcquireCloneTicket()
        self.fqdn        = attr_get( content.setting.setting, 'VirtualCenter.FQDN' )

        for arg in kwargs:
            setattr( self, arg, kwargs[ arg ] )

        vm = getattr( self, 'vm', None )
        if vm:
            self.vm_name = vm.name
            self.vm_id   = str( vm.id )


    def uri_vmrc( self, vm=None ):
        param = dict( vars( self ))
        if vm:
            param[ 'vm_name' ] = vm.name
            param[ 'vm_id' ]   = str( vm.id )
        return 'vmrc://clone:%(session)s@%(fqdn)s/?moid=%(vm_id)s' % param


    def uri_html( self, vm=None ):
        param = dict( vars( self ))
        if vm:
            param[ 'vm_name' ] = vm.name
            param[ 'vm_id' ]   = str( vm._moId )
        param[ 'html_host' ]   = getattr( self, 'html_host', self.host )
        param[ 'html_path' ]   = getattr( self, 'html_path', '/ui/webconsole.html' )
        param[ 'html_port' ]   = getattr( self, 'html_port', 9443 )
        uri = ( 'https://{html_host}:{html_port}{html_path}'
                +          '?vmId={vm_id}'
                +        '&vmName={vm_name}'
                +    '&serverGuid={serverGUID}'
                +          '&host={fqdn}'
                + '&sessionTicket={session}'
                +    '&thumbprint={fingerprint}' )
        return uri.format( **param )

# end class vomiMKS


######
## vmomi utility routines
######

def attr_get( obj, name ):
    for elt in obj:
        if getattr( elt, 'key' ) == name:
            return getattr( elt, 'value' )

def attr_to_dict( obj ):
    return dict( ( getattr( o, 'key' ),
                   getattr( o, 'value' ) )
                 for o in obj )

def propset_get( propset, name ):
    if type( propset ) is vmodl.query.PropertyCollector.ObjectContent:
        propset = propset.propSet
    for elt in propset:
        if elt.name == name:
            return elt.val

def propset_to_dict( propset ):
    if type( propset ) is vmodl.query.PropertyCollector.ObjectContent:
        propset = propset.propSet
    return dict( ( p.name, p.val ) for p in propset )

def get_seq_type( obj, typeref ):
    return filter( lambda elt: isinstance( elt , typeref ), obj )

def flat_to_nested_dict( flat, sep='.' ):
    nested = {}
    for k in flat:
        parts = k.split( sep )
        walk = nested
        for elt in parts[ :-1 ]:
            try:
                walk = walk[ elt ]
            except KeyError:
                walk[ elt ] = {}
                walk = walk[ elt ]
        walk[ parts[-1] ] = flat[ k ]
    return nested


class pseudoPropAttr( object ):
    # Allows foo.x to be retrieved as foo[ 'x' ]
    def __getitem__( self, key ):
        try:
            return getattr( self, key )
        except AttributeError as e:
            raise KeyError( e )

    def __setitem__( self, key, val ):
        setattr( self, key, val )
        return val # allow passthrough assignment a = b[ c ] = d

def flat_dict_to_nested_attributes( flat, sep='.' ):
    nested = pseudoPropAttr()
    for k in flat:
        parts = k.split( sep )
        walk = nested
        for elt in parts[ :-1 ]:
            try:
                walk = getattr( walk, elt )
            except AttributeError:
                setattr( walk, elt, pseudoPropAttr() )
                walk = getattr( walk, elt )
        setattr( walk, parts[-1], flat[ k ] )
    return nested


######
## generic utility routines
######

# This doesn't just use the textwrap class because we do a few special
# things here, such as avoiding filling command examples
def fold_text( text, maxlen=75, indent=0 ):
    text = text.expandtabs( 8 )

    text      = re.sub( "\r", '', text )        # CRLF -> LF
    paragraph = re.split( "\n\n", text, flags=re.M ) # Split into separate chunks.

    re_ll = re.compile( '(.{1,%s})(?:\s+|$)' % maxlen, flags=re.M )
    filled = []
    for para in paragraph:
        if re.match( '^\s*[#$]', para ):
            filled.append( para )
            continue

        # Remove all newlines, replacing trailing/leading
        # whitespace with a single space.
        #para = re.sub( "\\s*\n\\s*", ' ', para, flags=re.M )
        # Only unfill if line is >= 42 chars
        para = re.sub( "(?<=\S{42})\\s*\n\\s*", ' ', para, flags=re.M )

        # split into lines no longer than maxlen but only at whitespace.
        para = re.sub( re_ll, "\\1\n", para )
        # but remove final newline
        para = re.sub( "\n+$", '', para, flags=re.M )
        filled.append( para )

    text = str.join( "\n\n", filled ) # rejoin paragraphs at the end.
    if indent:
        repl = "\n" + (' ' * indent)
        text = re.sub( "\n", repl, text, flags=re.M )

    return text


def scale_size( size, fmtsize=1024 ):
    # x & (x-1) == 0 iff x == 2^n
    # if x == 2^n, only nth bit in x is set.
    # subtracting 1 flips all bits via a borrow; the logical AND is zero.
    # If x != 2^n, x-1 will flip all bits up to and including the first 1, but
    # will not negate the entire value and an AND will not produce zero.
    def pow2p( n ):
        return (n & (n - 1) == 0)

    if size == 0:
        return "0 B"

    suffix = ('', 'K', 'M', 'G', 'T', 'P', 'E')
    idx = 0

    ispow2 = pow2p (size)
    if not pow2p (size) or not pow2p (fmtsize):
        size = float( size )

    while size > fmtsize:
        size = size / fmtsize
        idx += 1

    if ispow2 and fmtsize == 1024:
        fmtstr = "%d %s%s"
        if size < 10: # Prefer "4096M" to "4G"
            size *= fmtsize
            idx -= 1
    elif size < 100 and idx > 0:
        fmtstr = "%.2f %s%s"
    else:
        size = round( size )
        fmtstr = "%d %s%s"

    if pow2p( fmtsize ): unit = "iB"
    else:                unit =  "B"

    return fmtstr % (size, suffix[idx], unit)

def printerr( *args, **kwargs ):
    sep  = kwargs.get( 'sep',  ': ' )
    end  = kwargs.get( 'end',  "\n" )
    file = kwargs.get( 'file', sys.stderr )

    pargs = list( args )
    if progname:
        pargs.insert( 0, progname )
    print( *pargs, sep=sep, end=end, file=file )


def y_or_n_p( prompt, yes='y', no='n', response=None, default=None ):
    if response is None:
        response = { 'y' : True,  'n' : False }
    c = " ({} or {}) "
    if default is None:
        choice = c.format( yes, no )
    elif default is False or default == no:
        choice = c.format( yes, no.upper() )
        default = False
    elif default is True or default == yes:
        choice = c.format( yes.upper(), no )
        default = True
    prompt = prompt + choice

    try:
        while True:
            print( prompt, end='' )
            ans = raw_input().lower()
            if ans in response:
                return response[ans]
            elif ans == '' and default is not None:
                return default
            print( 'Please answer {} or {}.'.format( yes, no ) )
    except KeyboardInterrupt:
        print( '\n\x57\x65\x6c\x6c\x20\x66\x75\x63\x6b',
               '\x79\x6f\x75\x20\x74\x68\x65\x6e\x2e\n' )
        sys.exit( 130 ) # WIFSIGNALED(128) + SIGINT(2)


def yes_or_no_p( prompt, default=None ):
    return y_or_n_p( prompt,
                     yes='yes',
                     no='no',
                     response={ 'yes' : True, 'no' : False },
                     default=default )

# eof
