# vspherelib.py --- convenience functions for vsphere client applications

# Author: Noah Friedman <friedman@splode.com>
# Created: 2017-10-31
# Public domain

# $Id: vspherelib.py,v 1.17 2018/07/08 08:03:01 noah Exp $

# Commentary:
# Code:

from __future__ import print_function

import argparse
import getpass
import os
import sys
import ssl
import re
import time

from pyVim   import connect as pyVconnect
from pyVmomi import vim, vmodl

# Get the id for a managed object type: Folder, Datacenter, Datastore, etc.
vim.ManagedObject.id = property( lambda self: self._moId )


class _timer( object ):
    enabled = os.getenv( 'VSPHERELIB_DEBUG' ) is not None

    def __init__( self, label ):
        if not self.enabled: return
        self.label = label
        self.start = time.clock()

    def report( self ):
        if not self.enabled: return
        end   = time.clock()
        total = end - self.start
        print( '{0}: {1}s'.format( self.label, total ), file=sys.stderr )

# end class _timer


class ArgumentParser( argparse.ArgumentParser, object ):
    class _Option(): pass  # just a container

    searchpath = ['XDG_CONFIG_HOME', 'HOME']
    rcname     = '.vspherelibrc.py'

    def __init__( self ):
        super( self.__class__, self ).__init__()
        opt = self.loadrc()
        self.add_argument( '-s', '--host',     default=opt.host,           help='Remote esxi/vcenter host to connect to' )
        self.add_argument( '-o', '--port',     default=opt.port, type=int, help='Port to connect on' )
        self.add_argument( '-u', '--user',     default=opt.user,           help='User name for host connection' )
        self.add_argument( '-p', '--password', default=opt.password,       help='Server user password' )

    # The rc file can manipulate this 'opt' variable; for example it could
    # provide a default for the host via:
    # 	opt.host = 'vcenter1.mydomain.com'
    def loadrc( self ):
        opt          = self._Option()
        opt.host     = None
        opt.port     = 443
        opt.user     = os.getenv( 'LOGNAME' )
        opt.password = None

        if os.getenv( 'VSPHERELIBRC' ):
            try:
                execfile( os.getenv( 'VSPHERELIBRC' ) )
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
            printerr( 'Server host is required' )
            sys.exit( 1 )

        if args.password:
            return args
        elif os.getenv( 'VMPASSWD' ):
            args.password = os.getenv( 'VMPASSWD' )
        else:
            prompt = 'Enter password for %(user)s@%(host)s: ' % vars( args )
            args.password = getpass.getpass( prompt )

        return args

# end class ArgumentParser


######
### retrieve managed objects by name
### This is meant to extend vmomiConnector, not used directly.
######

class _vmomiFinder( object ):
    def _get_single( self, name, mot, label, root=None ):
        """If name is null but there is only one object of that type anyway, just return that."""
        def err( msg, *res ):
            printerr( msg )
            printerr( 'Available {0}s:'.format( label ) )

            if res[0] and type( res[0] ) is vim.ManagedObject.Array:
                res = res[0]
            else:
                res = self.get_obj( mot )
            names = [elt.name for elt in res]
            for n in sorted( names ):
                printerr( "\t" + n )
            exit( 1 )

        if name:
            if type( root ) is vim.ManagedObject.Array:
                res = filter( lambda o: o.name == name, root )
            else:
                res = self.get_obj( mot, { 'name' : name }, root=root )

            if res is None or len( res ) < 1:
                err( name, '{0} not found or not available.'.format( label ) )
            if len( res ) > 1:
                err( name, 'name is not unique.', res )
        else:
            if type( root ) is vim.ManagedObject.Array:
                res = root
            else:
                res = self.get_obj( mot, root=root )

            if res is None or len( res ) < 1:
                err( 'No {0}s found!'.format( label ) )
            if len( res ) > 1:
                err( 'More than one {0}s exists; specify {0}s to use.'.format( label ), res )
        return res[0]

    def get_datacenter( self, name, root=None ):
        return self._get_single( name, [vim.Datacenter], 'datacenter', root=root )

    def get_cluster( self, name, root=None ):
        return self._get_single( name, [vim.ComputeResource], 'cluster', root=root )

    def get_datastore( self, name, root=None ):
        return self._get_single( name, [vim.Datastore], 'datastore', root=root )

    def get_pool( self, name, root=None ):
        return self._get_single( name, [vim.ResourcePool], 'resource pool', root=root )

    def get_vm( self, name, root=None ):
        return self._get_single( name, [vim.VirtualMachine], 'virtual machine', root=root )

    # TODO: for hosts which still can't be found from the searchindex,
    # try a substring match on all host names.
    def find_vm( self, *names ):
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
                args.remove( vm[ 'name' ] )

        if len( args ) > 0:
            search = self.si.content.searchIndex
            for vmname in args:
                i = sortord.index( vmname )
                sortord.pop( i )

                res = search.FindByDnsName( vmSearch=True, dnsName=vmname )
                if res:
                    found.append( res )
                    sortord.insert( i, res.name )
                else:
                    res = search.FindByIp( vmSearch=True, ip=vmname )
                    if res:
                        found.append( res )
                        sortord.insert( i, res.name )

        self.vmlist_sort_by_args( found, sortord )
        return found

    def vmlist_sort_by_args( self, vmlist, args ):
        if not vmlist or len(vmlist) < 1:
            return
        vmorder = dict()
        i = 0
        for name in args:
            vmorder[ name ] = i
            i += 1
        cmp_fn = lambda a, b: cmp( vmorder[ a.name ], vmorder[ b.name ] )
        if type( vmlist[ 0 ] ) is vmodl.query.PropertyCollector.ObjectContent:
            cmp_fn = lambda a, b: cmp( vmorder[ a.obj.name ], vmorder[ b.obj.name ] )
        vmlist.sort( cmp=cmp_fn )

# end class vmomiFinder


######
### vCenter folder-related routines
### This is meant to extend vmomiConnector, not used directly.
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
        except AttributeError:
            self._init_path_folder_maps()
            return self._folder_to_path_map[ folder ]

    # Return the folder object located at path
    def path_to_folder( self, path ):
        try:
            return self._path_to_folder_map[ path ]
        except AttributeError:
            self._init_path_folder_maps()
            return self._path_to_folder_map[ path ]

    # legacy: get path of folder in which vm resides
    def get_vm_folder_path( self, vm ):
        return self.folder_to_path( vm.parent )

# end class vmomiFolderMap


######
### resolve network labels and distributed port groups
### This is meant to extend vmomiConnector, not used directly.
######

class _vmomiNetworkMap( object ):
    def get_network_groupmap( self ):
        tbl = {}
        nets = self.get_obj_props( [vim.dvs.DistributedVirtualPortgroup], ['config'] )
        for elt in nets:
            conf = elt[ 'config' ]
            tbl[ conf.key ] = conf.name
        return tbl

    def get_network_label( self, nic ):
        if hasattr( nic.backing, 'deviceName' ):
            return nic.backing.deviceName
        groupmap = self.get_network_groupmap()
        if issubclass( type( nic.backing ),
                       vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo ):
            key = nic.backing.port.portgroupKey
            return groupmap.get( key, key )
        return 'unknown'

    # a vmnic is any vim.vm.device.VirtualEthernetCard type element
    # from vm.config.hardware.device
    def get_vmnic_cidrs( self, vmnic ):
        """
        a vmnic is any vim.vm.device.VirtualEthernetCard type element from
        vm.config.hardware.device
        """
        if not vmnic.ipConfig:
            return
        cidr = []
        for ip in vmnic.ipConfig.ipAddress:
            cidr.append( ip.ipAddress + "/" + str( ip.prefixLength ) )
        return cidr

# end of class vmomiNetworkMap



class vmomiConnector( _vmomiFinder, _vmomiFolderMap, _vmomiNetworkMap ):
    def __init__( self, *args, **kwargs ):
        if len( args ) == 1 and issubclass( type( args[0] ), argparse.Namespace ):
            kwargs = self.namespacetodict( args[0] )
        self.host = kwargs[ 'host' ]
        self.user = kwargs[ 'user' ]
        self.pwd  = kwargs[ 'password' ]
        self.port = int( kwargs.get( 'port', 443 ))
        self.connect()
        self._network_groupmap = None

    def namespacetodict( self, namespace ):
        darg = {}
        for pair in namespace._get_kwargs():
            darg[ pair[0] ] = pair[1]
        return darg

    def __del__( self ):
        try:
            pyVconnect.Disconnect( self.si )
        except AttributeError:
            pass


    def connect( self ):
        timer = _timer( 'vmomiConnector.connect' )
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
            printerr( args.host, 'Could not connect to ESXi/vCenter server' )
            printerr( repr( e ) )
            sys.exit( 1 )
        timer.report()


    def create_filter_spec( self, container, props ):
        if type( props ) is dict:
            props = props.keys()
        elif type( props ) is str:
            props = [ props ]

        vpc        = vmodl.query.PropertyCollector
        travSpec   = vpc.TraversalSpec( name='traverseEntities',
                                        path='view',
                                        skip=False,
                                        type=type( container ) )
        objSpec    = vpc.ObjectSpec( obj=container, skip=True, selectSet=[ travSpec ] )
        propSet    = [ vpc.PropertySpec( type=t,
                                         pathSet=props,
                                         all=not props or not len( props ) )
               for t in set( type(v) for v in container.view ) ]
        filterSpec = vpc.FilterSpec( objectSet=[ objSpec ], propSet=propSet )
        return filterSpec


    def create_container_view( self, type, root=None, recursive=True ):
        if root is None:
            root = self.si.content.rootFolder
        return self.si.content.viewManager.CreateContainerView(
            container = root,
            type      = type,
            recursive = recursive )


    def create_list_view( self, *objs ):
        return self.si.content.viewManager.CreateListView( obj=objs )


    def get_obj_props( self, vimtype, props=None, root=None, recursive=True ):
        container    = None
        gc_container = False
        if any( map( lambda c: issubclass( type( vimtype ), c),
                     ( vim.view.ListView, vim.view.ContainerView ))):
            container = vimtype
        else:
            container = self.create_container_view( vimtype, root=root, recursive=recursive )
            gc_container = True

        result = None
        if props is None:
            result = container.view
        else:
            spc = self.si.content.propertyCollector
            filterSpec = self.create_filter_spec( container, props )
            # Would need to loop with this.
            #opt  = vmodl.query.PropertyCollector.RetrieveOptions()
            #pres = spc.RetrievePropertiesEx( specSet=[ filterSpec ], options=opt )
            timer = _timer( 'spc.RetrieveContents' )
            res = spc.RetrieveContents( [ filterSpec ] )
            timer.report()
            if res:
                match = res
                if type( props ) is dict:
                    match = []
                    for r in res:
                        for rprop in r.propSet:
                            want = props.get( rprop.name )
                            if want is None:
                                continue
                            if ( (type( want ) is str  and rprop.val == want )
                                 or
                                 (type( want ) is list and rprop.val in want ) ):
                                match.append( r )
                                break
                if len( match ) > 0:
                    result = []
                    for m in match:
                        elt = propset_to_dict( m.propSet )
                        elt[ 'obj' ] = m.obj
                        result.append( elt )
        if gc_container:
            container.Destroy()
        return result


    def get_obj( self, *args, **kwargs):
        result = self.get_obj_props( *args, **kwargs )
        if not result:
            return
        if kwargs.get( 'props' ) or len( args ) > 1:
            return [ elt[ 'obj' ] for elt in result ]
        else:
            return result


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
        propSpec   =  vpc.PropertySpec( type=vim.Task, pathSet=[], all=True )
        filterSpec =  vpc.FilterSpec( objectSet=objSpecs, propSet=[ propSpec ] )
        filter     =  spc.CreateFilter( filterSpec, True )

        succ     = 1
        taskleft = [ task.info.key for task in tasklist ]
        try:
            version, state = None, None

            while len( taskleft ):
                update = spc.WaitForUpdates( version )

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
                version = update.version
        finally:
            if filter:
                filter.Destroy()
        return succ

# end class vmomiConnect


######
## vmomi utility routines
######

def attr_get( obj, name ):
    for elt in obj:
        if getattr( elt, 'key' ) == name:
            return getattr( elt, 'value' )

def attr_to_dict( obj ):
    attrs = dict()
    for elt in obj:
        key = getattr( elt, 'key' )
        val = getattr( elt, 'value' )
        attrs[ key ] = val
    return attrs

def propset_get( propset, name ):
    if type( propset ) is vmodl.query.PropertyCollector.ObjectContent:
        propset = propset.propSet
    search = filter( lambda elt: elt.name == name, propset )
    if search and len( search ) > 0:
        return search[ 0 ].val

def propset_to_dict( propset ):
    if type( propset ) is vmodl.query.PropertyCollector.ObjectContent:
        propset = propset.propSet
    _dict = dict()
    for prop in propset:
        _dict[ prop.name ] = prop.val
    return _dict

def get_seq_type( obj, typeref ):
    return filter( lambda elt: issubclass( type( elt ), typeref ), obj)


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
    print( *args, sep=sep, end=end, file=sys.stderr )


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
