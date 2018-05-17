# vspherelib.py --- convenience functions for vsphere client applications

# Author: Noah Friedman <friedman@splode.com>
# Created: 2017-10-31
# Public domain

# $Id: vspherelib.py,v 1.13 2018/05/14 22:48:07 friedman Exp $

# Commentary:
# Code:

from __future__ import print_function

import argparse
import atexit
import getpass
import os
import sys
import ssl
import re
import time

from pyVim   import connect as pyVconnect
from pyVmomi import vim, vmodl


class _timer:
    enabled = os.getenv( 'VSPHERELIB_DEBUG' ) is not None

    def __init__( self, label ):
        if not self.enabled: return
        self.label = label
        self.start = time.clock()

    def report( self ):
        if not self.enabled: return
        end   = time.clock()
        total = end - self.start
        print( '{0}: {1}s'.format( self.label, total ))


class MyArgumentParser( argparse.ArgumentParser ):
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

def get_args_setup():
    return MyArgumentParser()


def hconnect( args ):
    timer = _timer( 'hconnect' )
    try:
        context = None
        if hasattr( ssl, '_create_unverified_context' ):
            context = ssl._create_unverified_context()

        si = pyVconnect.SmartConnect( host = args.host,
                                      user = args.user,
                                      pwd  = args.password,
                                      port = int( args.port ),
                                      sslContext = context )

    except Exception as e:
        printerr( args.host, 'Could not connect to ESXi/vCenter server' )
        printerr( repr( e ) )
        sys.exit( 1 )

    timer.report()
    atexit.register( pyVconnect.Disconnect, si )
    return si


def create_filter_spec( container, props ):
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


def create_container_view( si, type, root=None, recursive=True ):
    if root is None:
        root = si.content.rootFolder
    return si.content.viewManager.CreateContainerView(
        container = root,
        type      = type,
        recursive = recursive )

def create_list_view( si, *objs ):
    return si.content.viewManager.CreateListView( obj=objs )


def get_obj_props( si, vimtype, props=None, root=None, recursive=True ):
    container    = None
    gc_container = False
    if any( map( lambda c: issubclass( type( vimtype ), c),
                 ( vim.view.ListView, vim.view.ContainerView ))):
        container = vimtype
    else:
        container = create_container_view(
            si, vimtype, root=root, recursive=recursive )
        gc_container = True

    result = None
    if props is None:
        result = container.view
    else:
        spc = si.content.propertyCollector
        filterSpec = create_filter_spec( container, props )
        # Would need to loop with this.
        #opt  = vmodl.query.PropertyCollector.RetrieveOptions()
        #pres = spc.RetrievePropertiesEx( specSet=[ filterSpec ], options=opt )
        timer = _timer( 'RetrieveContents' )
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


def get_obj( *args, **kwargs):
    result = get_obj_props( *args, **kwargs )
    if not result:
        return
    if kwargs.get( 'props' ) or len( args ) > 2:
        return [ elt[ 'obj' ] for elt in result ]
    else:
        return result


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


def taskwait( si, tasklist, printsucc=True, callback=None ):
    class our(): pass
    our.callback = callback

    def diag_callback( *args ):
        print( args, file=sys.stderr )
        # Perhaps we can do something more useful here depending on the
        # type of error.  For now, just stop further callbacks
        our.callback = None

    spc = si.content.propertyCollector
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


# TODO: for hosts which still can't be found from the searchindex,
# try a substring match on all host names.
def vmlist_find( si, *names ):
    args = None # make copy of names since we alter
    if type( names[0] ) is not str:
        args = list( names[0] )
    else:
        args = list( names )
    sortord = list( args )
    found = []

    vmlist = get_obj_props( si, [vim.VirtualMachine], { 'name' : args } )
    if vmlist:
        for vm in vmlist:
            found.append( vm[ 'obj' ] )
            args.remove( vm[ 'name' ] )

    if len( args ) > 0:
        search = si.content.searchIndex
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

    vmlist_sort_by_args( found, sortord )
    return found


def vmlist_sort_by_args( vmlist, args ):
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


# Generate a complete map of full paths to corresponding vsphere folder objects
def path_to_folder_map( si ):
    mtbl = {}
    for elt in get_obj_props( si, [vim.Folder, vim.Datacenter], ['name', 'parent'] ):
        moId = repr( elt[ 'obj' ] )
        mtbl[ moId ] = [ repr( elt[ 'parent' ] ), elt[ 'name' ], elt ]

    rootFolder  = si.content.rootFolder
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

_folder_to_path_map = None
_path_to_folder_map = None
def _init_path_folder_maps( si ):
    global _path_to_folder_map
    global _folder_to_path_map
    p2f = path_to_folder_map( si )
    f2p = { p2f[k] : k for k in p2f }
    _path_to_folder_map = p2f
    _folder_to_path_map = f2p

# Return the path name of the folder object
def folder_to_path( si, folder ):
    global _folder_to_path_map
    if not _folder_to_path_map:
        _init_path_folder_maps ( si )
    return _folder_to_path_map[ folder ]

# Return the folder object located at path
def path_to_folder( si, path ):
    global _path_to_folder_map
    if not _path_to_folder_map:
        _init_path_folder_maps ( si )
    return _path_to_folder_map[ path ]

# legacy: get path of folder in which vm resides
def get_vm_folder_path( si, vm ):
    return folder_to_path( si, vm.parent )


def get_network_groupmap( si ):
    tbl = {}
    nets = get_obj_props( si, [vim.dvs.DistributedVirtualPortgroup], ['config'] )
    for elt in nets:
        conf = elt[ 'config' ]
        tbl[ conf.key ] = conf.name
    return tbl

_groupmap = None
def get_network_label( si, nic ):
    if hasattr( nic.backing, 'deviceName' ):
        return nic.backing.deviceName

    global _groupmap
    if not _groupmap:
        _groupmap = get_network_groupmap( si )

    if issubclass( type( nic.backing ),
                   vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo ):
        key = nic.backing.port.portgroupKey
        if key in _groupmap:
            return _groupmap[ key ]
        else:
            return key
    return 'unknown'


# a vmnic is any vim.vm.device.VirtualEthernetCard type element
# from vm.config.hardware.device
def get_vmnic_cidrs( vmnic ):
    if not vmnic.ipConfig:
        return
    cidr = []
    for ip in vmnic.ipConfig.ipAddress:
        cidr.append( ip.ipAddress + "/" + str( ip.prefixLength ) )
    return cidr


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
