# vspherelib.py --- convenience functions for vsphere client applications

# Author: Noah Friedman <friedman@splode.com>
# Created: 2017-10-31
# Public domain

# Commentary:
# Code:

from __future__ import print_function

import __builtin__
import argparse
import getpass
import os
import sys
import OpenSSL
import ssl
import re
import time
import atexit
import functools
import threading
import random
import weakref

from fnmatch    import translate as glob2regex

from pyVim      import connect as pyVconnect
from pyVmomi    import vim, vmodl

import requests
requests.packages.urllib3.disable_warnings()

# Get the id for a managed object type: Folder, Datacenter, Datastore, etc.
vim.ManagedObject.id = property( lambda self: self._moId )

try:
    progname = os.path.basename( sys.argv[0] )
except:
    pass


POSIX = object()  # posix system, e.g. unix or osx
WinNT = object()  # MICROS~1
Undef = object()  # distinct default from None since None is hashable

# WOW32, WOW64, WOWNative; methods use Native by default
wowBitness = vim.vm.guest.WindowsRegistryManager.RegistryKeyName.RegistryKeyWowBitness

debug = bool( os.getenv( 'VSPHERELIB_DEBUG' ))


def with_conditional_stacktrace( *exceptions ):
    def print_exception( exc, val, sta ):
        args = [os.path.basename( sys.argv[0] ) or exc.__name__]
        try:
            args.append( val.object.name )
        except AttributeError:
            pass
        try:
            args.append( val.msg )
        except AttributeError:
            args.append( str( val ) )
        print( *args, sep=': ', file=sys.stderr )
        exclude = [ 'dynamicType',
                    'dynamicProperty',
                    'faultCause',
                    'faultMessage',
                    'object',
                    'windowsSystemErrorCode',
                    'msg',
                    'reason', ]
        for attr in sorted( val.__dict__ ):
            if attr not in exclude:
                print( attr, getattr( val, attr ), sep=' = ', file=sys.stderr )

    def excepthook( exc, val, sta ):
        sys.excepthook = sys.__excepthook__
        if not debug and issubclass( exc, tuple( exceptions )):
            print_exception( exc, val, sta )
        else:
            return sys.__excepthook__( exc, val, sta )

    def class_decorator( wrapped_class ):
        class conditional_stacktrace_wrapper( wrapped_class ):
            def __init__( self, *args, **kwargs ):
                sys.excepthook = excepthook
                self.reason = str.join( ': ', (str( s ) for s in args) )
            def __str__( self ):
                return self.reason
        conditional_stacktrace_wrapper.__name__ = wrapped_class.__name__
        return conditional_stacktrace_wrapper

    def decorator( fn ):
        if getattr( fn, '__bases__', None ):
            return class_decorator( fn )
        else:
            @functools.wraps( fn )
            def wrapper( *args, **kwargs ):
                sys.excepthook = excepthook
                return fn( *args, **kwargs )
            return wrapper

    return decorator

def tidy_vimfaults( fn ):
    decorator = with_conditional_stacktrace(
        vim.fault.GuestComponentsOutOfDate,
        vim.fault.GuestOperationsUnavailable,
        vim.fault.GuestRegistryKeyInvalid,
        vim.fault.NoPermission,
        vim.fault.InvalidGuestLogin,
        vmodl.MethodFault, )
    return decorator( fn )

def conditional_stacktrace_exception( wrapped_class ):
    decorator = with_conditional_stacktrace( wrapped_class )
    return decorator( wrapped_class )

# These should always produce a stacktrace, even in non-debug mode
class vmomiError( Exception ): pass
class ApiError( vmomiError ):  pass

# These should not
@conditional_stacktrace_exception
class vmomiErrorCST(         vmomiError ):    pass
class cliGeneralError(       vmomiErrorCST ): pass
class NameNotFoundError(     vmomiErrorCST ): pass
class NameNotUniqueError(    vmomiErrorCST ): pass
class ConnectionFailedError( vmomiErrorCST ): pass
class RequiredArgumentError( vmomiErrorCST ): pass
class GuestOperationError(   vmomiErrorCST ): pass


class Diag( object ):
    def __init__( self, *args, **kwargs ):
        self.sep    = kwargs.get( 'sep',  ': ' )
        self.lines  = []
        self.append( *args )

    def __str__( self ):
        if not self.lines:
            return ''
        return str.join( '\n', self.lines )

    def append( self, *args ):
        if args:
            self.lines.append( self.sep.join( args ))


# This is just a dictionary but you can access
# or assign elements as either d['x'] or d.x

class pseudoPropAttr( dict ):
    def __setattr__( self, name, value ):
        self[ name ] = value
        return value

    def __getattr__( self, name ):
        try:
            return self[ name ]
        except KeyError as e:
            raise AttributeError( *e.args )

    def __delattr__( self, name ):
        try:
            del self[ name ]
        except KeyError as e:
            raise AttributeError( *e.args )

# end class pseudoPropAttr


class Timer( object ):
    enabled  = bool( os.getenv( 'VSPHERELIB_TIMER' ))
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
        print( Timer.fmt.format( 'TOTAL', Timer.acc_cl, Timer.acc_tm ),
               file=Timer.fh )
    atexit.register( _atexit_report_accum )

# end class Timer


######
## ArgumentParser subclass with special handling
######

# stub for supporting `with' statements
class _with( object ):
    def __enter__( self ):
        return self

    def __exit__( self, *exc_info ):
        try:
            self.__del__()
        except:
            pass

class _super( object ):
    super = property( lambda self: super( type( self ), self ) )

class ArgumentParser( argparse.ArgumentParser, _super, _with ):
    searchlist = [ ['XDG_CONFIG_HOME', 'vspherelibrc.py'],
                   ['HOME',           '.vspherelibrc.py'], ]

    class _SubParsersAction( argparse._SubParsersAction, _super ):
        def add_parser( self, *args, **kwargs ):
            'Overload: copy description from help if the former is not specified'
            kwargs.setdefault( 'description', kwargs.get( 'help', None ) )
            return self.super.add_parser( *args, **kwargs)

        def alias( self, new, existing, force=False ):
            if not force and new in self._name_parser_map:
                raise NameNotUniqueError( new, 'attempt to redefine existing subparser.' )
            self._name_parser_map[ new ] = self._name_parser_map[ existing ]


    def __init__( self, loadrc=False, rest=None, help='Remaining arguments', required=False, **kwargs ):
        self.is_subparser = bool( not loadrc )
        self.super.__init__( **kwargs )

        # override the default subparser so we can insert default
        # descriptions based on help strings..
        self.register( 'action', 'parsers', self._SubParsersAction )

        if self.is_subparser:
            return

        timer = Timer( 'loadrc' )
        self.opt = self.loadrc()
        timer.report()

        nargs = '*'
        if required:
            if type( required ) is bool:
                nargs = '+'
            else:
                nargs = required
            if not rest:
                rest = 'rest'

        self.add( '-s', '--host',     required=True,           help='Remote esxi/vcenter host to connect to' )
        self.add( '-o', '--port',     required=True, type=int, help='Port to connect on' )
        self.add( '-u', '--user',     required=True,           help='User name for host connection' )
        self.add( '-p', '--password', required=False,          help='Server user password' )
        if rest:
            self.add( rest, nargs=nargs,                       help=help )

    # The rc file can manipulate this 'opt' variable; for example it could
    # provide a default for the host via:
    # 	opt.host = 'vcenter1.mydomain.com'
    def loadrc( self ):
        opt          = pseudoPropAttr()
        opt.host     = None
        opt.port     = 443
        opt.user     = os.getenv( 'LOGNAME' )
        opt.password = None

        _environ = dict( globals() )
        _environ.update( locals() )
        def source( filename ):
            script = file_contents( filename )
            script.replace( '\r\n', '\n' )
            exec( script, _environ, _environ )

        env_rc = os.getenv( 'VSPHERELIBRC' )
        if env_rc:
            try:
                source( env_rc )
            except IOError:
                pass
        else:
            for elt in self.searchlist:
                try:
                    source( os.getenv( elt[0] ) + '/' + elt[1] )
                    break
                # TypeError can result if envvar is unset
                except (TypeError, IOError):
                    continue
        return opt

    def _arg_dest_default( self, *args ):
        for optname in args:
            if optname.find( '--' ) != 0:
                continue
            return optname[ 2: ].replace( '-', '_' )
        return args[0][ 1: ]

    def add_argument( self, *args, **kwargs ):
        if not self.is_subparser:
            # Inject any values from rc file into defaults
            name = self._arg_dest_default( *args )
            try:
                kwargs[ 'default' ] = self.opt[ name ]
                try:
                    del kwargs[ 'required' ]
                except KeyError:
                    pass
            except (KeyError, AttributeError):
                pass
        return self.super.add_argument( *args, **kwargs )
    add = add_argument # alias

    def add_bool( self, *args, **kwargs ):
        kwargs.setdefault( 'action', 'store_true' )
        kwargs.setdefault( 'default', None )
        return self.add_argument( *args, **kwargs )

    def add_mxbool( self, opt_true, opt_false, help_true=None, help_false=None, **kwargs ):
        'Add mutually exclusive pair of boolean arguments'
        if not isinstance( opt_true,  list ): opt_true  = [opt_true]
        if not isinstance( opt_false, list ): opt_false = [opt_false]

        kwargs.setdefault( 'dest',    self._arg_dest_default( *opt_true ) )
        kwargs.setdefault( 'default', None )
        if kwargs.get( 'help', None ):
            if help_true  is None: help_true  = kwargs[ 'help' ]
            if help_false is None: help_false = kwargs[ 'help' ]
            del kwargs[ 'help' ]

        group = self.add_mutually_exclusive_group()
        group.add_argument( *opt_true,  action='store_true',  help=help_true, **kwargs )
        group.add_argument( *opt_false, action='store_false', help=help_false, **kwargs )

    def parse_args( self ):
        args = self.super.parse_args()
        if self.is_subparser:
            return args

        if not args.host:
            raise RequiredArgumentError( 'Server host is required' )

        if args.password:
            pass
        elif os.getenv( 'VMPASSWD' ):
            args.password = os.getenv( 'VMPASSWD' )
        else:
            prompt = 'Enter password for %(user)s@%(host)s: ' % vars( args )
            args.password = getpass.getpass( prompt )

        extra = self.opt
        for elt in extra:
            if elt.find( '_' ) != 0 and not hasattr( args, elt ):
                setattr( args, elt, extra[ elt ] )

        return args
    parse = parse_args # alias

# end class ArgumentParser


######
## Class for handling property list parameters in a more systematic way.
######

class propList( object ):
    def __new__( self, *args ):
        '''If first param is already an instance, just return previous instance'''
        # n.b. in __new__, self is a class, not an instance
        if isinstance( args[0], self ):
            return args[0]
        else:
            return super( self, self ).__new__( self, *args )

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
                for key in elt:
                    try:
                        kvl = self.propdict[ key ]
                    except KeyError:
                        kvl = self.propdict[ key ] = []

                    val = elt[ key ]
                    if type( val ) in (tuple, list):
                        kvl.extend( val )
                    else:
                        kvl.append( val )
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


class Cache( object ):
    valid_table_methods = [ 'get', 'has_key', 'keys', 'values', 'items' ]
    ttl = 60

    def __init__( self, **kwargs ):
        if kwargs.get( 'ttl', None ):
            self.ttl = int( kwargs[ 'ttl' ] )
        self.table = {}
        self.timer = {}
        self.mutex = threading.Lock()

        for method in self.valid_table_methods:
            setattr( self, method, getattr( self.table, method ))

        # For CPython versions 3.1 or earlier, explicitly shut down daemon
        # threads at exit, because otherwise they keep running even as the
        # interpreter is busy destroying objects around them, resulting in
        # exceptions in the threading module after the program has
        # otherwise terminated.  In 3.2 and later, daemon threads are
        # frozen at interpreter shutdown time.
        if ( sys.version_info.major < 3
             or ( sys.version_info.major == 3
                  and sys.version_info.minor < 2 )):
            weak = weakref.ref( self )
            atexit.register( lambda: weak() and weak().thread_cleanup() )

    def thread_cleanup( self ):
        for thr in self.timer.values():
            thr.cancel()
            thr.join()

    # Note: caller must acquire lock before calling this.
    def __deltimer( self, k ):
        try:
            timer = self.timer[ k ]
            del self.timer[ k ]
            if timer.ident != threading.current_thread().ident:
                timer.cancel()
                timer.join()
        except KeyError:
            pass

    def __getitem__( self, k ):
        return self.table[ k ]

    def __setitem__( self, k, v ):
        self.mutex.acquire()
        try:
            self.__deltimer( k )
            self.table[ k ] = v
            self.timer[ k ] = threading.Timer(
                self.ttl, self.__delitem__, args=[ k ] )
            # No need to finish this thread if main thread exits
            self.timer[ k ].daemon = True
            self.timer[ k ].start()
        finally:
            self.mutex.release()
        return v # for passthrough

    def __delitem__( self, k ):
        self.mutex.acquire()
        try:
            if debug:
                v = self.table[ k ]
                printerr( 'debug', self,
                          'expire {1:#x} {0!r}'.format( k, id( v )) )
            del self.table[ k ]
            self.__deltimer( k )
        except KeyError:
            pass
        finally:
            self.mutex.release()


##
## Mixins for collecting managed objects and properties
##

class _vmomiCollect( object ):
    def create_filter_spec( self, vimtype, container, props ):
        props    = propList( props or [] )

        vpc      = vmodl.query.PropertyCollector
        travSpec = vpc.TraversalSpec( name = 'traverseEntities',
                                      path = 'view',
                                      skip = False,
                                      type = type( container ) )
        objSpec  = vpc.ObjectSpec( obj=container, skip=True, selectSet=[ travSpec ] )
        propSet  = [ vpc.PropertySpec( type    = vimt,
                                       pathSet = props.names(),
                                       all     = bool( not props ) )
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

    def _get_obj_props_nofilter( self, vimtype,
                                 props = None,
                                 root = None,
                                 recursive = True,
                                 ignoreInvalidProps = False ):
        '''
        Retrieve all listed properties from objects in container (root), or
        create container out of the rootFolder.

        Returns an object of type vmodl.query.PropertyCollector.ObjectContent[],
        or vim.ManagedObject[] if there are no properties to collect.
        '''

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
            while True:
                try:
                    result = spc.RetrieveProperties( [ filterSpec ] )
                    break
                except vmodl.query.InvalidProperty as e:
                    if not ignoreInvalidProps:
                        raise
                    # This is not ideal for collectors on multiple object types.
                    # If a property path was invalid, on *which* object type was it invalid?
                    # The exception doesn't give us any information, so we remove it from all
                    # object types in the filterspec.
                    for propSet in filterSpec.propSet:
                        propSet.pathSet.remove( e.name )
                    if debug:
                        printerr( 'warning', e.name, 'invalid property path' )
            timer.report()

        if gc_container:
            container.Destroy()
        return result

    def get_obj_props( self, vimtype, props=None, root=None, recursive=True, mustMatchAll=True, ignoreInvalidProps=False ):
        '''
        If any of the properties have matching values to search for, narrow down
        the view of managed objects to retrieve the full list of attributes from.

        So for example, if you want a laundry list of attributes from a
        VirtualMachine but only the one that has a specific name, it's way
        faster to create a new container with just that machine in it
        before then collecting a dozen properties from it.

        Returns a list of dict objects for each result.

        '''
        if not props:
            return self._get_obj_props_nofilter( vimtype, props, root, recursive, ignoreInvalidProps )

        # First get the subset of managed objects we want
        props   = propList( props )
        filters = props.filters()
        if filters:
            filterprops = filters.keys()
            # Don't skip invalid properties here even if requested since
            # these particular ones have match conditions attached.
            res = self._get_obj_props_nofilter( vimtype, filterprops, root, recursive )
            match = []

            # By default, only include results for which every property name
            # has a value included in the list of wanted values for that property.
            # Otherwise, include results if any match is found in any property.
            timer = Timer( 'filter preliminary results' )
            if mustMatchAll:
                for r in res:
                    for rprop in r.propSet:
                        if rprop.val not in filters.get( rprop.name ):
                            break
                    else: # loop did not call break
                        match.append( r )
            else:
                for r in res:
                    for rprop in r.propSet:
                        if rprop.val in filters.get( rprop.name ):
                            match.append( r )
                            break
            timer.report()
            if not match:
                # there were filters but nothing matched.  So don't let the
                # collector below run since it would collect *everything*
                return
        else:
            # No filters, so fetch everything in the container
            match = root

        # Now get all the props from the selected objects... if there are
        # any we don't already have.  (propnames is a superset of filters)
        #
        # TODO: don't refetch any properties we already have.  Strip them
        # out, then merge this new set with the prior ones.
        propnames = props.names()
        if len( filters ) != len( propnames ):
            res = self._get_obj_props_nofilter( vimtype, propnames, match, recursive, ignoreInvalidProps )
        else:
            res = match

        if res:
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

    @staticmethod
    def clone_obj( obj, *attrs ):
        """
        Create a shallow copy of object and its attributes.
        Attribute values are shared, not copied.
        """
        if not attrs:
            exclude = ['dynamicProperty', 'dynamicType']
            attrs = filter( lambda s: s not in exclude, obj.__dict__ )
        new = type( obj )()
        for attr in attrs:
            if hasattr( obj, attr ):
                setattr( new, attr, getattr( obj, attr ))
        return new


# end class _vmomiCollect


##
## mixins for retrieving Managed Objects by name
##

class _vmomiFind( object ):
    def name_to_mo_map( self, typelist, root=None ):
        if root is None:
            root = self.si.content.rootFolder
        typestr  = str.join( ', ', sorted( [elt.__name__ for elt in typelist] ))
        map_name = 'name to mo map: type=[{}] root={}'.format( typestr, root._moId )
        try:
            return self.cache[ map_name ]
        except KeyError:
            pass

        result = {}
        mo_list = self._get_obj_props_nofilter( typelist, ['name'], root=root )
        for mo in mo_list:
            name = mo.propSet[0].val
            try:
                result[ name ].append( mo.obj )
            except KeyError:
                result[ name ] = [ mo.obj ]
        self.cache[ map_name ] = result
        return result

    def _get_single( self, name, mot, label, root=None ):
        '''If name is null but there is only one object of that type anyway, just return that.'''
        def err( exception, msg, res=root ):
            try:
                names = [ elt.name for elt in res ]
                if len( set( names ) ) != len( names ):
                    # Names are not unique; show their object id.
                    names = [ '{} ({})'.format( elt.name, elt._moId )
                              for elt in res ]
            except TypeError as e:
                if not res or isinstance( res, vim.ManagedObject ):
                    names = self.name_to_mo_map( mot, res ).keys()
                else:
                    names = res

            diag = Diag( msg )
            if True or names:
                local_label = label
                if local_label[-2:] == 'ch':
                    local_label += 'e'
                diag.append( 'Available {0}s:'.format( local_label ) )
                for n in sorted( names ):
                    diag.append( '\t' + n )
            raise exception( diag )

        if name:
            if isinstance( root, vim.ManagedObject.Array ):
                found = filter( lambda o: o.name == name, root )
            else:
                try:
                    found = self.name_to_mo_map( mot, root )[ name ]
                except KeyError:
                    found = None

            if not found:
                 err( NameNotFoundError, '{}: {} not found or not available.'.format( name, label ) )
            elif len( found ) > 1:
                err( NameNotUniqueError, '{}: name is not unique.'.format( name ), found )
        else:
            if isinstance( root, vim.ManagedObject.Array ):
                found = root
            else:
                found = []
                for val in self.name_to_mo_map( mot, root ).values():
                    found.extend( val )

            if not found:
                raise NameNotFoundError( 'No {0}s found!'.format( label ))
            elif len( found ) > 1:
                if mot[ 0 ] is vim.ResourcePool and len( mot ) == 1:
                    # "Resources" pools are children of (cluster)ComputeResource objects.
                    # If there is just one other resource pool other than those kind, return that.
                    childpools = filter( lambda elt: isinstance( elt.parent, vim.ResourcePool ), found )
                    if childpools and len( childpools ) == 1:
                        return childpools[0]

                err( NameNotUniqueError,
                     'More than one {0} exists; specify {0} to use.'.format( label, label ),
                     found )
        return found[0]

    def get_datacenter( self, name, root=None ):
        return self._get_single( name, [vim.Datacenter], 'datacenter', root=root )

    # results may include ClusterComputeResource
    def get_compute_resource( self, name, root=None ):
        return self._get_single( name, [vim.ComputeResource], 'compute resource', root=root )
    get_cluster = get_compute_resource # legacy

    # excludes ComputeResource objects
    def get_cluster_compute_resource( self, name, root=None ):
        return self._get_single( name, [vim.ClusterComputeResource], 'cluster compute resource', root=root )

    def get_host( self, name, root=None ):
        return self._get_single( name, [vim.HostSystem], 'host', root=root )

    def get_datastore( self, name, root=None ):
        return self._get_single( name, [vim.Datastore], 'datastore', root=root )

    def get_resource_pool( self, name, root=None ):
        return self._get_single( name, [vim.ResourcePool], 'resource pool', root=root )
    get_pool = get_resource_pool # legacy

    def get_network( self, name, root=None ):
        return self._get_single( name, [vim.Network], 'network label', root=root )

    # These are a subset of vim.Network
    def get_portgroup( self, name, root=None ):
        return self._get_single( name, [vim.dvs.DistributedVirtualPortgroup], 'portgroup', root=root )

    def get_dvswitch( self, name, root=None ):
        return self._get_single( name, [vim.DistributedVirtualSwitch], 'distributed virtual switch', root=root )

    def get_vm( self, name, root=None ):
        try:
            return self._get_single( name, [vim.VirtualMachine], 'virtual machine', root=root )
        except:
            search = self.find_vm( name, root=root, showerrors=False )
            if len( search ) != 1:
                raise
            else:
                return search[0]

    def find_vm( self, *names, **kwargs ):
        args = None # make copy of names since we alter
        if type( names[0] ) is not str:
            args = list( names[0] )
        else:
            args = list( names )

        root   = kwargs.get( 'root', None )
        vm_map = self.name_to_mo_map( [vim.VirtualMachine], root )
        def find_by_name( name ):
            try:
                return vm_map[ name ]
            except KeyError:
                pass

        idx = self.si.content.searchIndex
        searchfns = [
            lambda pat: idx.FindAllByUuid(    vmSearch=True,    uuid=pat ),
            lambda pat: idx.FindAllByIp(      vmSearch=True,      ip=pat ),
            lambda pat: find_by_name( pat ),
            lambda pat: idx.FindAllByDnsName( vmSearch=True, dnsName=pat ),
            lambda pat: self.search_by_name( pat ) ]

        found    = []
        notfound = []
        for name in args:
            for fn in searchfns:
                try:
                    res = fn( name )
                    if res:
                        found.extend( res )
                        break
                except vmodl.fault.SystemError:
                    # ESXi 4.x doesn't like uuid searches with
                    # non-conforming patterns
                    pass
            else:
                notfound.append( name )

        if kwargs.get( 'showerrors', True ):
            for name in notfound:
                    printerr( '"{}"'.format( name ), 'virtual machine not found.' )

        return found

    def search_by_name( self, name, objtype=vim.VirtualMachine, regex=False ):
	'''Return a list of managed objects with name matching NAME.

	NAME is treated as a shell-style glob pattern or a list of
        patterns, in which case objects which match any one of them are
        included in the results.

	Optional keyword arg OBJTYPE specifies one or more managed object
        types to include.  It defaults to `vim.VirtualMachine'.

	Optional keyword arg REGEX may be `True' or a set of regex compiler
        flags (e.g. `re.I', `re.M', etc. logically ORed together) in which
        case the NAME pattern(s) are all treated as regular expressions
        instead of glob patterns.

        Note: if you know the exact name of an object, it's more efficient
        to retrieve it using the `get_vm', `get_datastore', etc. methods or
        the more general `get_obj' method, since they take advantage of
        server-side pruning and indexing.  This method has to perform a
        full search of the requested managed object categories in order to
        perform pattern matching.

        '''
        if not isinstance( objtype, list ):
            objtype = [ objtype ]
        if not isinstance( name, list ):
            name = [ name ]

        result = []
        objmap = self.name_to_mo_map( objtype ) # n.b. temporarily cached
        for nelt in name:
            isglob = any( c in nelt for c in '?*!~[' ) if not regex else False

            if not (isglob or regex):
                try:
                    result.extend( objmap[ nelt ] )
                except KeyError:
                    pass
            else:
                pat = '^' + glob2regex( nelt ) + '$' if isglob else nelt
                # If regex is actually a set of flags (e.g. re.I), pass that to compiler.
                pat = re.compile( pat, 0 if isinstance( regex, bool ) else regex )
                for oelt in objmap:
                    if pat.search( oelt ) is not None:
                        result.extend( objmap[ oelt ] )
        return result

# end class _vmomiFinder


##
## folder-related mixins
##

class _vmomiFolderMap( object ):
    # Generate a complete map of paths to server folder objects.
    # These are cached because round trips to the server are slow.
    def _init_folder_path_maps( self ):
        mtbl = {}
        for elt in self.get_obj_props( [vim.Folder, vim.Datacenter],
                                       ['name', 'parent'] ):
            obj = elt[ 'obj' ]
            mtbl[ obj ] = [ elt[ 'name' ], elt[ 'parent' ] ]
        p2f = {}
        f2p = {}
        for obj in mtbl:
            name = []
            start_obj = obj
            while mtbl.has_key( obj ):
                node = mtbl[ obj ]
                name.insert( 0, node[ 0 ] )
                obj = node[ 1 ]
                # See if we've already computed the rest of the parent path.
                # If so, prepend it and stop.
                try:
                    name.insert( 0, f2p[ obj ] )
                    break
                except KeyError:
                    pass
            if name:
                if name[0][0] is not '/':
                    name.insert( 0, '' )
                name = str.join( '/', name )
                p2f[ name ]      = start_obj
                f2p[ start_obj ] = name
        self.cache[ 'path_to_folder_map' ] = p2f
        self.cache[ 'folder_to_path_map' ] = f2p

    def _folder_path_map( self, attr, item=Undef ):
        try:
            mapping = self.cache[ attr ]
        except KeyError:
            self._init_folder_path_maps()
            mapping = self.cache[ attr ]
        if item is Undef:
            return mapping
        else:
            try:
                return mapping[ item ]
            except KeyError:
                pass

    def folder_to_path_map( self, item=Undef ):
        return self._folder_path_map( 'folder_to_path_map', item )

    def path_to_folder_map( self, item=Undef ):
        return self._folder_path_map( 'path_to_folder_map', item )

    # Prunes folder tree to just the vm, host, network, datastore,
    # etc. subtree.  The datacenter is still prefixed to all the folders
    # but the intermediate subfolder name and all other folders are removed.
    # The default 'vm' subtree is usually the only interesting one.
    #
    # To get the inverse of this map, use the 'inverted_dict' function below.
    def path_to_subfolder_map( self, subfolder='vm' ):
        p2sf = {}
        for path, obj in self.path_to_folder_map().items():
            try:
                beg = path.index( '/', 1 )
            except ValueError:
                continue

            try:
                end = path.index( '/', beg + 1 )
            except ValueError:
                end = len( path )

            sub = path[ beg + 1 : end ]
            if sub != subfolder:
                continue

            sfpath = path[ 0 : beg ] + path[ end : ]
            p2sf[ sfpath ] = obj
        return p2sf

# end class _vmomiFolderMap


##
## Network mixins
## resolves network labels and distributed port groups
##

class _vmomiNetworkMap( object ):
    def _get_network_moId_label_map( self ):
        try:
            return self.cache[ 'network_moId_label_map' ]
        except KeyError:
            nets = self._get_obj_props_nofilter( [vim.Network], ['name'] )
            mapping = {  x.obj._moId : x.propSet[ 0 ].val for x in nets }
            self.cache[ 'network_moId_label_map' ] = mapping
            return mapping

    def get_nic_network_label( self, nic ):
        try:
            return nic.backing.deviceName
        except AttributeError:
            pass

        try:
            groupKey = nic.backing.port.portgroupKey
        except AttributeError:
            return
        mapping = self._get_network_moId_label_map()
        return mapping.get( groupKey, groupKey )

    def get_portgroup_switchUUID( self, label, host=None ):
        if not host:
            host = self.get_obj( [vim.HostSystem] )
        elif type( host ) is vim.HostSystem:
            host = [ host ]
        elif type( host ) is vim.VirtualMachine:
            host = [ host.runtime.host ]

        dvs_mgr = self.si.content.dvSwitchManager
        for obj in host:
            ct = dvs_mgr.QueryDvsConfigTarget( host=obj )
            for pg in ct.distributedVirtualPortgroup:
                if label in (pg.portgroupName, pg.portgroupKey):
                    return pg.switchUuid

# end of class _vmomiNetworkMap


##
## changeSpec mixins
##
class _vmomiChangeSpec( object ):
    def make_device_connection_changespec(
            self,
            vm,
            label,
            connect             = None,
            start_connected     = None,
            allow_guest_control = None ):
        dev = filter( lambda elt: elt.deviceInfo.label == label,
                      vm.config.hardware.device )
        if not dev:
            raise NameNotFoundError(
                '{}: "{}" device not found'.format( vm.name, label ))

        devspec           = vim.vm.device.VirtualDeviceSpec()
        devspec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
        devspec.device    = dev[0]

        c = devspec.device.connectable
        c.connected         = connect
        c.startConnected    = start_connected
        c.allowGuestControl = allow_guest_control
        return devspec

    def make_disk_format_changespec( self, vm, dest_format, index=None ):
        vd = vim.vm.device.VirtualDisk
        devspecs  = vim.vm.RelocateSpec.DiskLocator.Array()
        src_disks = get_seq_type( vm.config.hardware.device,
                                  vim.vm.device.VirtualDisk )
        if index is not None:
            src_disks = [ src_disks[ index ] ]

        for src in src_disks:
            dspec = vim.vm.RelocateSpec.DiskLocator()
            dspec.diskId = src.key
            if dest_format in ['sesparse']:
                dspec.diskBackingInfo = vd.SeSparseBackingInfo()
            else:
                dspec.diskBackingInfo = vd.FlatVer2BackingInfo()
                if dest_format in ['thin']:
                    dspec.diskBackingInfo.thinProvisioned = True
                elif dest_format in ['thick', 'zeroedthick']:
                    pass
                elif dest_format in ['eagerzeroedthick']:
                    dspec.diskBackingInfo.eagerlyScrub = True
            devspecs.append( dspec )
        return devspecs

    def make_disk_resize_changespec( self, vm, disknum, size ):
        try:
            disknum = int( disknum )
            disklabel = 'Hard disk {}'.format( disknum )
        except ValueError:
            disklabel = disknum

        disk = filter( lambda n: n.deviceInfo.label == disklabel,
                       get_seq_type( vm.config.hardware.device,
                                     vim.vm.device.VirtualDisk ))
        if not disk:
            raise NameNotFoundError(
                '{}: "{}" disk not found'.format( vm.name, disklabel ))

        devspec           = vim.vm.device.VirtualDeviceSpec()
        devspec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
        devspec.device    = disk[0]
        #if mode:
        #    devspec.device.backing.diskMode = mode
        devspec.device.capacityInBytes = str_to_bytes( size )
        return devspec

    def make_nic_changespec( self, vm, label, index=0, root=None ):
        ethernet = vim.vm.device.VirtualEthernetCard
        nic = get_seq_type( vm.config.hardware.device, ethernet )[index]

        spec           = vim.vm.device.VirtualDeviceSpec()
        spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
        spec.device    = nic

        if label:
            net = self.get_network( label, root=root )
        else:
            net = None

        try:
            dvs_port = vim.dvs.PortConnection()

            if net:
                dvs_port.portgroupKey = net.key
                dvs_port.switchUuid   = self.get_portgroup_switchUUID( net.key, vm )
            else:
                dvs_port.portgroupKey = nic.backing.port.portgroupKey
                dvs_port.switchUuid   = nic.backing.port.switchUuid

            spec.device.backing = ethernet.DistributedVirtualPortBackingInfo()
            spec.device.backing.port = dvs_port

        except AttributeError:
            spec.device.backing = ethernet.NetworkBackingInfo()
            if net:
                spec.device.backing.network    = net
                spec.device.backing.deviceName = label
            else:
                spec.device.backing.network    = nic.backing.network
                spec.device.backing.deviceName = nic.backing.deviceName

        return spec


##
## GuestInfo subtype mixins
##

class _vmomiGuestInfo( object ):
    def vmguest_dns_config( self, vm ):
        dns = []
        for ipStack in vm.guest.ipStack:
            dconf = ipStack.dnsConfig
            if not dconf:
                continue
            elt = { 'dhcp'     : dconf.dhcp,
                    'hostname' : dconf.hostName,
                    'domain'   : dconf.domainName,
                    'server'   : list( dconf.ipAddress ),
                    'search'   : list( dconf.searchDomain ), }

            if elt[ 'domain' ] and elt[ 'domain' ][-1] == '.':
                elt[ 'domain' ] = elt[ 'domain' ][:-1]

            search = elt[ 'search' ]
            for i in range( 0, len( search )):
                if len( search[i] ) > 0 and search[ i ][ -1 ] == '.':
                    search[ i ] = search[ i ][:-1]

            dns.append( elt )
        return dns

    def vmguest_ip_routes( self, vm ):
        tbl = {}

        # guest net device numbers might not be in the same order as vmx nic order.
        # We want to return entries in nic order.
        netOrder = { mac : i  for i, mac
                         in enumerate( net.macAddress for net in vm.guest.net ) }
        nicOrder = [ netOrder.get( nic.macAddress, None )
                     for nic in get_seq_type( vm.config.hardware.device,
                                              vim.vm.device.VirtualEthernetCard ) ]
        for ipStack in vm.guest.ipStack:
            routes = ipStack.ipRouteConfig.ipRoute
            for elt in routes:
                if ( elt.prefixLength == 128
                     or elt.network in ['ff00::', '169.254.0.0']
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

                dev = int( elt.gateway.device )
                try:
                    eth = tbl[ dev ]
                except KeyError:
                    eth = tbl[ dev ] = []
                eth.append( new )
        return [ tbl.get( n, [] ) for n in nicOrder ]

    def vmguest_ip_addrs( self, vm ):
        return [ self.vmnic_cidrs( vmnic )
                 for vmnic in vm.guest.net ]

    # a vmnic is any vim.vm.device.VirtualEthernetCard type element
    # from vm.config.hardware.device
    def vmnic_cidrs( self, vmnic ):
        '''
        vmnic should be an object of type vim.vm.GuestInfo.NicInfo


        a vmnic is any vim.vm.device.VirtualEthernetCard type element from
        vm.config.hardware.device
        '''
        if vmnic.ipConfig:
            return [ '{}/{}'.format( ip.ipAddress, ip.prefixLength )
                     for ip in vmnic.ipConfig.ipAddress ]
        else:
            return vmnic.ipAddress

    def vmguest_nic_info( self, vm ):
        pst      = vim.VirtualMachine.PowerState
        ethernet = vim.vm.device.VirtualEthernetCard
        nics = []
        for nic in get_seq_type( vm.config.hardware.device, ethernet ):
            prop = { 'obj'        : nic,
                     'type'       : nic._wsdlName.replace( 'Virtual', '' ).lower(),
                     'label'      : nic.deviceInfo.label,
                     'netlabel'   : self.get_nic_network_label( nic ),
                     'macAddress' : nic.macAddress,
                     'backing'    : nic.backing, }
            if vm.summary.runtime.powerState == pst.poweredOn:
                gnic = filter( lambda g: g.macAddress.lower() == nic.macAddress.lower(),
                               vm.guest.net )
                if gnic:
                    prop[ 'ip' ] = self.vmnic_cidrs( gnic[0] )
            nics.append( prop )
        return nics

    def vmguest_disk_info( self, vm ):
        controller = { elt.key : elt for elt in
                       get_seq_type( vm.config.hardware.device,
                                     vim.vm.device.VirtualController ) }
        d_layout = {}
        for disk in vm.layoutEx.disk:
            fileKey = []
            for chain in disk.chain:
                fileKey.extend( chain.fileKey )
            d_layout[ disk.key ] = fileKey
        f_layout = { elt.key : elt.size for elt in vm.layoutEx.file }

        vd = vim.vm.device.VirtualDisk
        vm_disk_list = []
        for disk in get_seq_type( vm.config.hardware.device, vd ):
            alloc = sum( f_layout[ key ] for key in d_layout[ disk.key ] )

            ctrl = controller[ disk.controllerKey ]
            devlabel = str.split( ctrl.deviceInfo.label, ' ', 2 )[0].lower()
            dev = '{}{}:{}'.format( devlabel, ctrl.busNumber, disk.unitNumber )

            backing = disk.backing

            prop = { 'obj'       : disk,
                     'label'     : disk.deviceInfo.label,
                     # esxi4 may not fill out capacityInBytes
                     'capacity'  : disk.capacityInBytes or disk.capacityInKB * 1024,
                     'allocated' : alloc,
                     'device'    : dev,
                     'fileName'  : backing.fileName,
                     'diskMode'  : backing.diskMode, }

            if isinstance( backing, vd.FlatVer2BackingInfo ):
                if backing.thinProvisioned:
                    prop[ 'backing' ] = 'thin'
                elif backing.eagerlyScrub:
                    prop[ 'backing' ] = 'eagerzeroedthick'
                else:
                    prop[ 'backing' ] = 'zeroedthick'
            elif isinstance( backing, vd.SeSparseBackingInfo ):
                prop[ 'backing' ] = 'sesparse'
            elif isinstance( backing, vd.RawDiskMappingVer1BackingInfo ):
                prop[ 'deviceName' ] = backing.deviceName
                prop[ 'backing' ] = 'rawdiskmapping'
            else:
                beg = len( 'VirtualDisk' )
                end = len( 'BackingInfo' )
                prop[ 'backing' ] = backing._wsdlName[ beg : -end ].lower()

            try:
                vflash = disk.vFlashCacheConfigInfo
                prop[ 'vflash_reserve' ] = vflash.reservationInMB * 2**20
                prop[ 'vflash_blksz'   ] = vflash.blockSizeInKB   * 2**10
            except AttributeError:
                pass

            vm_disk_list.append( prop )
        return vm_disk_list


##
## Monitor-related mixins
##

class _vmomiMonitor( object ):
    # This method will keep running until the callback returns any value other than 'None',
    # or an exception occurs (including any unhandled exception in the callback).
    # Otherwise the return value is the final return value of the callback.
    def monitor_property_changes( self, objlist, proplist, callback ):
        spc = self.si.content.propertyCollector
        vpc = vmodl.query.PropertyCollector

        types = set( type( obj ) for obj in objlist )
        if isinstance( objlist, ( vim.view.ListView, vim.view.ContainerView )):
            container    = objlist
            gc_container = False
        elif isinstance( objlist, list ):
            container    = self.create_list_view( objlist )
            gc_container = True
        else:
            container    = self.create_container_view( types, objlist )
            gc_container = True

        try:
            filter_spec = self.create_filter_spec( types, container, proplist )
            filter_obj  = spc.CreateFilter( filter_spec, True )

            result, version  = None, None
            while result is None:
                update = spc.WaitForUpdatesEx( version )
                for filterSet in update.filterSet:
                    for objSet in filterSet.objectSet:
                        for change in objSet.changeSet:
                            result = callback( change, objSet, filterSet, update )
                            if result is not None:
                                return result
                version = update.version
        finally:
            try:
                # might be unbound if an error occurs in create_filter_spec
                filter_obj.Destroy()
            except NameError:
                pass
            if gc_container:
                container.Destroy()

    def taskwait( self, *args, **kwargs ):
        return vmomiTaskWait( self, *args, **kwargs ).wait()

# end class _vmomiMonitor


##
## The main class, the entry point for everything else
##

class vmomiConnect( _vmomiCollect,
                    _vmomiFind,
                    _vmomiFolderMap,
                    _vmomiNetworkMap,
                    _vmomiChangeSpec,
                    _vmomiGuestInfo,
                    _vmomiMonitor,
                    _with ):

    def __init__( self, *args, **kwargs ):
        kwargs = dict( **kwargs ) # copy; destructively modified
        for arg in args:
            if isinstance( arg, argparse.Namespace ):
                kwargs.update( vars( arg ))

        self.host   = kwargs[ 'host' ]
        self.port   = int( kwargs.get( 'port', 443 ))
        self.user   = kwargs[ 'user' ]
        self.pwd    = kwargs[ 'password' ]
        del kwargs[ 'password' ]
        # When idle is negative, no timeout is enabled.
        # The default is (last I checked) 900s.
        self.idle   = int( kwargs.get( 'idle', pyVconnect.CONNECTION_POOL_IDLE_TIMEOUT_SEC ))
        self.kwargs = kwargs
        self.cache  = Cache( ttl=kwargs.get( 'cacheTimeout', None ) )
        self.connect()

    def __del__( self ):
        self.close()

    def close( self ):
        try:
            pyVconnect.Disconnect( self.si )
            self.si = None
        except:
            pass

    def connect( self ):
        timer = Timer( 'vmomiConnect.connect' )
        try:
            try:
                sslContext = ssl._create_unverified_context()
            except AttributeError:
                sslContext = None

            # These stubs enable automatic reconnection if a session times out.
            smart_stub = pyVconnect.SmartStubAdapter(
                                 host = self.host,
                                 port = self.port,
                connectionPoolTimeout = self.idle,
                           sslContext = sslContext )
            vsos = pyVconnect.VimSessionOrientedStub
            login_method = vsos.makeUserLoginMethod( self.user, self.pwd )
            session_stub = vsos( smart_stub, login_method )
            self.si = vim.ServiceInstance( 'ServiceInstance', session_stub )

            # SmartStubAdapter doesn't seem to work with ESXi 4.1 (or earlier?)
            try:
                self.si.content
            except (vmodl.fault.MethodNotFound, vmodl.fault.SystemError) as e:
                #printerr( 'warning', 'using non-reconnectable connection mechanism' )
                self.si = pyVconnect.SmartConnect(
                        host = self.host,
                        port = self.port,
                        user = self.user,
                        pwd  = self.pwd,
                        sslContext=sslContext )

        except Exception as e:
            msg = ': '.join(( self.host,
                              'Could not connect',
                              getattr( e, 'msg', str( e ) ) ))
            raise ConnectionFailedError( msg )
        timer.report()

    def session_cookie( self ):
        return self.si._stub.soapStub.cookie

    def vmguest_ops( self, vm, *args, **kwargs ):
        return vmomiVmGuestOperation( self, vm, *args, **kwargs )

    def datastore_file_ops( self, *args, **kwargs ):
        return vmomiDataStoreFile( self, *args, **kwargs )

    def mks( self, *args, **kwargs ):
        return vmomiMKS( self, *args, **kwargs )

# end class vmomiConnect


######
## Remote console session ticket class
######

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

        # This is a dumb thing to fault on, but overly restrictive
        # permissions might prevent us from inspecting vcenter to form a
        # preferred url.
        try:
            self.fqdn = attr_get( content.setting.setting, 'VirtualCenter.FQDN' )
        except vmodl.fault.SecurityError:
            pass
        # Might be None for an esxi session
        if not self.fqdn:
            self.fqdn = self.host

        for arg in kwargs:
            setattr( self, arg, kwargs[ arg ] )

        vm = getattr( self, 'vm', None )
        if vm:
            self.vm_name = vm.name
            self.vm_id   = str( vm._moId )


    def uri_vmrc( self, vm=None ):
        param = dict( vars( self ))
        if vm:
            param[ 'vm_name' ] = vm.name
            param[ 'vm_id' ]   = str( vm._moId )
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


class vmomiDataStoreFile( _with ):
    session  = property( lambda self: self._get_session() )
    ds_regex = re.compile( '^\[\s*([^)]+)\s*\]\s*(.*)$' )

    def __init__( self, vsi, *args, **kwargs ):
        if len( args ) == 1:
            if kwargs.get( 'dsName', None ):
                kwargs.setdefault( 'path', args[ 0 ] )
            else:
                match = self.ds_regex.search( args[ 0 ].strip() )
                if match:
                    dsName, path = match.groups()
                    kwargs.setdefault( 'dsName', dsName )
                    kwargs.setdefault( 'path',   path )
                else:
                    raise RequiredArgumentError( args[0], 'unparsable path' )
        elif len( args ) == 2:
            kwargs.setdefault( 'dsName', args[ 0 ] )
            kwargs.setdefault( 'path',   args[ 1 ] )
        elif args:
            raise RequiredArgumentError( args, 'Too many args' )

        self.vsi          = vsi
        self.dsName       = kwargs[ 'dsName' ]
        self.path         = kwargs[ 'path' ]
        self.useHostAgent = kwargs.get( 'useHostAgent', True )
        self.stream       = kwargs.get( 'stream',       True )
        self.chunk_size   = kwargs.get( 'chunk_size',   16384 )

        if isinstance( self.dsName, vim.Datastore ):
            self.datastore = self.dsName
            self.dsName    = self.datastore.name
        else:
            self.datastore = self.vsi.get_datastore( self.dsName )

    def __del__( self ):
        try:
            self._session.close()
        except:
            pass

    def _get_session( self ):
        try:
            return self._session
        except AttributeError:
            sess = self._session = requests.Session()
            sess.stream = True
            sess.verify = False
            sess.headers.update( {
                'Content-Type' : 'application/octet-stream',
            } )
            c_name, c_val = self.vsi.session_cookie().split( '=', 1 )
            sess.cookies.update( { c_name : c_val } )
            return self._session

    def _ds_datacenter( self ):
        if self.useHostAgent:
            return 'ha-datacenter' # this is constant for ESXi hosts
        else:
            dc = self.datastore.parent
            while not isinstance( dc, vim.Datacenter ):
                dc = dc.parent
            return dc.name

    def _ds_host( self ):
        if self.useHostAgent:
            host = self.datastore.host
            n = random.randint( 1, len( host ) )
            return host[ n-1 ].key.name
        else:
            return self.vsi.host

    # A URL has the form
    #
    #     scheme://authority/folder/path?dcPath=dcPath&dsName=dsName
    #
    # where
    # 	* scheme: http or https.
    # 	* authority: hostname or IP of the vcenter/esxi host+port
    # 	* dcPath: Datacenter containing the Datastore
    # 	* dsName: name of the Datastore
    # 	* path: slash-delimited path from the root of the datastore
    def _mkUrl( self ):
        p = self.path
        url = '{scheme}://{host}:{port}/folder/{path}?' \
              'dcPath={dcPath}&dsName={dsName}'
        return url.format( scheme = 'https',
                             host = self._ds_host(),
                             port = 443,
                             path = p[ 1: ] if p[ 0 ] == '/' else p,
                           dcPath = self._ds_datacenter(),
                           dsName = self.dsName, )

    @tidy_vimfaults
    def _mkticket( self, url, method='httpGet' ):
        spec = vim.SessionManager.HttpServiceRequestSpec()
        spec.url    = url
        spec.method = method
        sm = self.vsi.si.content.sessionManager
        ticket = sm.AcquireGenericServiceTicket( spec=spec )
        return { 'vmware_cgi_ticket' : ticket.id }

    # Return a wrapped iterator which closes the response object
    # after all chunks are returned.
    def _response_generator( self, resp ):
        def generate():
            try:
                for chunk in resp.iter_content( chunk_size=self.chunk_size ):
                    yield chunk
            finally:
                resp.close()
        return generate()

    def get( self ):
        url = self._mkUrl()
        if debug:
            printerr( 'debug', 'GET', url )

        if self.useHostAgent:
            resp = self.session.get( url, cookies=self._mkticket( url ) )
        else:
            resp = self.session.get( url )

        if not resp.ok:
            raise NameNotFoundError(
                '{} {}: "[{}] {}"'.format(
                    resp.status_code, resp.reason,
                    self.dsName, self.path ) )

        if self.stream:
            return self._response_generator( resp )
        else:
            try:
                return resp.content
            finally:
                resp.close()


######
## Mixins for vmomiVmGuestOperation class, used to perform tasks within a
## virtual machine
######

class _vmomiVmGuestOperation_Env( object ):
    @tidy_vimfaults
    def guest_environ( self ):
        try:
            return self._guest_environ
        except AttributeError:
            env = self.pmgr.ReadEnvironmentVariableInGuest(
                vm    = self.vm,
                auth  = self.auth )
            self._guest_environ = environ_to_dict( env, preserve_case=False )
            return self._guest_environ

    def getenv( self, name ):
        try:
            return self.guest_environ()[ name ]
        except KeyError:
            pass

# end class _vmomiVmGuestOperation_Env


class _vmomiVmGuestOperation_Dir( object ):
    @tidy_vimfaults
    def mkdir( self, path, mkdirhier=False ):
        self._printdbg( 'mkdir', path )
        self.fmgr.MakeDirectoryInGuest(
            vm                      = self.vm,
            auth                    = self.auth,
            directoryPath           = path,
            createParentDirectories = mkdirhier )

    @tidy_vimfaults
    def mkdtemp( self, prefix='', suffix='', directoryPath=None ):
        tmpdir = self.fmgr.CreateTemporaryDirectoryInGuest(
            vm            = self.vm,
            auth          = self.auth,
            prefix        = prefix,
            suffix        = suffix,
            directoryPath = directoryPath )
        self._printdbg( 'mkdtemp', tmpdir )
        self.tmpdir.append( tmpdir )
        return tmpdir

    @tidy_vimfaults
    def mvdir( self, src, dst ):
        self._printdbg( 'mvdir', src, dst )
        self.fmgr.MoveDirectoryInGuest(
            vm               = self.vm,
            auth             = self.auth,
            srcDirectoryPath = src,
            dstDirectoryPath = dst )

    @tidy_vimfaults
    def rmdir( self, directoryPath, recursive=False ):
        if recursive:
            self._printdbg( 'rmdir -r', directoryPath )
        else:
            self._printdbg( 'rmdir', directoryPath )

        try:
            self.tmpdir.remove( directoryPath )
        except ValueError:
            pass

        self.fmgr.DeleteDirectoryInGuest(
            vm            = self.vm,
            auth          = self.auth,
            directoryPath = directoryPath,
            recursive     = recursive )

# end class _vmomiVmGuestOperation_Dir


class _vmomiVmGuestOperation_File( object ):
    def _gc_tmpfiles( self, files=[], dirs=[] ):
        for elt in files:
            try:
                self.unlink( elt )
            except vim.fault.VimFault:
                pass
        for elt in dirs:
            try:
                self.rmdir( elt, recursive=True )
            except vim.fault.VimFault:
                pass

    _fileAttrMap = { 'uid'      : 'ownerId',
                     'gid'      : 'groupId',
                     'mode'     : 'permissions',
                     'atime'    : 'accessTime',
                     'mtime'    : 'modificationTime',
                     'ctime'    : 'createTime',
                     'hidden'   : 'hidden',
                     'readonly' : 'readOnly' }
    def mkFileAttributes( self, *args, **kwargs ):
        for arg in args:
            if isinstance( arg, dict ):
                kwargs.update( args )
            elif isinstance( arg, (int, long) ):
                kwargs[ 'mode' ] = arg

        if self.ostype is WinNT:
            attr = vim.vm.guest.FileManager.WindowsFileAttributes()
        else:
            attr = vim.vm.guest.FileManager.PosixFileAttributes()

        attrmap = self._fileAttrMap
        for k in attrmap:
            if kwargs.get( k ) is not None:
                setattr( attr, attrmap[ k ], kwargs[ k ] )
        return attr

    def decodeFileAttributes( self, attr ):
        # n.b. [acm]time are datetime objects; one way to convert
        # to unix epoch is: calendar.timegm( mtime.timetuple() )
        rec = pseudoPropAttr()
        attrmap = self._fileAttrMap
        for elt in attrmap:
            attrname = attrmap[ elt ]
            if getattr( attr, attrname, None ):
                rec[ elt ] = getattr( attr, attrname )
        symlink = getattr( attr, 'symlinkTarget', None )
        if symlink not in ( '', None ):
            rec[ 'symlink' ] = symlink
        return rec

    @tidy_vimfaults
    def ls( self, path=None, pattern='^.*', long=False, max=None, _fstat=False ):
        if path is None:
            path = self.cwd or '/'
        result    = []
        index     = 0
        remaining = max
        if not _fstat: # fstat can override
            self._printdbg( lambda: ('ls', path, '({})'.format( pattern )))
        while True:
            batch = self.fmgr.ListFilesInGuest(
                vm           = self.vm,
                auth         = self.auth,
                filePath     = path,
                index        = index,
                maxResults   = remaining,
                matchPattern = pattern )

            result.extend( batch.files )
            if batch.remaining == 0:
                break
            index     = len( result )
            remaining = batch.remaining

        if not long:
            return [ f.path for f in result ]

        record = []
        while result:
            st  = result.pop()
            elt = self.decodeFileAttributes( st.attributes )
            elt[ 'size' ] = st.size
            record.append( elt )
        return record

    def fstat( self, guestFilePath ):
        self._printdbg( 'fstat', guestFilePath )
        rec = self.ls( path=guestFilePath, long=True, max=1, _fstat=True )
        return rec[0]

    @tidy_vimfaults
    def chmod( self, path, **kwargs ):
        attr = self.mkFileAttributes( **kwargs )
        self._printdbg( 'chmod', attr, path )
        self.fmgr.ChangeFileAttributesInGuest(
            vm             = self.vm,
            auth           = self.auth,
            guestFilePath  = path,
            fileAttributes = attr )

    @tidy_vimfaults
    def mktemp( self, prefix='', suffix='', directoryPath=None ):
        tmpfile = self.fmgr.CreateTemporaryFileInGuest(
            vm            = self.vm,
            auth          = self.auth,
            prefix        = prefix,
            suffix        = suffix,
            directoryPath = directoryPath )
        self._printdbg( 'mktemp', tmpfile )
        self.tmpfile.append( tmpfile )
        return tmpfile

    @tidy_vimfaults
    def unlink( self, filePath ):
        self._printdbg( 'unlink', filePath )
        try:
            self.tmpfile.remove( filePath )
        except ValueError:
            pass
        self.fmgr.DeleteFileInGuest(
            vm       = self.vm,
            auth     = self.auth,
            filePath = filePath )

    @tidy_vimfaults
    def get_file( self, guestFile ):
        self._printdbg( 'get_file', guestFile )
        ftinfo = self.fmgr.InitiateFileTransferFromGuest(
            vm            = self.vm,
            auth          = self.auth,
            guestFilePath = guestFile )

        # TODO: verify the connection using the host cert.
        # The urllib3 interface requires certs to be stored in a file and
        # the location passed in, which another annoying setup nit.
        resp = requests.get( ftinfo.url, verify=False )
        if resp.status_code != 200:
            raise GuestOperationError( str( status_code ), resp.reason )
        return resp.text

    @tidy_vimfaults
    def put_file( self, filePath, data, perm=None, overwrite=False ):
        attr = self.mkFileAttributes( perm )
        self._printdbg( 'put_file', filePath, attr )
        url = self.fmgr.InitiateFileTransferToGuest(
            vm             = self.vm,
            auth           = self.auth,
            guestFilePath  = filePath,
            fileAttributes = attr,
            fileSize       = len( data ),
            overwrite      = overwrite )

        # TODO: verify the connection using the host cert.
        resp = requests.put( url, data=data, verify=False )
        if resp.status_code != 200:
            raise GuestOperationError( resp )

# end class _vmomiVmGuestOperation_File


class _vmomiVmGuestOperation_Registry( object ):
    REG_BINARY    = vim.vm.guest.WindowsRegistryManager.RegistryValueBinary
    REG_DWORD     = vim.vm.guest.WindowsRegistryManager.RegistryValueDword
    REG_QWORD     = vim.vm.guest.WindowsRegistryManager.RegistryValueQword
    REG_EXPAND_SZ = vim.vm.guest.WindowsRegistryManager.RegistryValueExpandString
    REG_MULTI_SZ  = vim.vm.guest.WindowsRegistryManager.RegistryValueMultiString
    REG_SZ        = vim.vm.guest.WindowsRegistryManager.RegistryValueString

    @staticmethod
    def _mkRegKeyNameSpec( path, wow=None ):
        return vim.vm.guest.WindowsRegistryManager.RegistryKeyName(
            registryPath = path,
            wowBitness   = wow or wowBitness.WOWNative )

    @classmethod
    def _mkRegValNameSpec( self, path, name, wow=None ):
        return vim.vm.guest.WindowsRegistryManager.RegistryValueName(
            keyName = self._mkRegKeyNameSpec( path, wow=None ),
            name    = name )

    @tidy_vimfaults
    def reg_keys_list( self, path, recursive=False, match='^.*', wow=None, native=False ):
        res = self.regmgr.ListRegistryKeysInGuest(
            vm           = self.vm,
            auth         = self.auth,
            keyName      = self._mkRegKeyNameSpec( path, wow ),
            recursive    = recursive,
            matchPattern = match )
        if native:
            return res
        else:
            return [ elt.key.keyName.registryPath for elt in res ]

    @tidy_vimfaults
    def reg_key_create( self, path, volatile=False, wow=None ):
        try:
            self.regmgr.CreateRegistryKeyInGuest(
                vm         = self.vm,
                auth       = self.auth,
                keyName    = self._mkRegKeyNameSpec( path, wow ),
                isVolatile = volatile )
        except vim.fault.GuestRegistryKeyAlreadyExists:
            pass
        except:
            raise

    @tidy_vimfaults
    def reg_key_delete( self, path, recursive=False, wow=None ):
        try:
            self.regmgr.DeleteRegistryKeyInGuest(
                vm        = self.vm,
                auth      = self.auth,
                keyName   = self._mkRegKeyNameSpec( path, wow ),
                recursive = recursive )
        except vim.fault.GuestRegistryKeyInvalid:
            # TODO: this case can also occur if the path leading up to the
            # key is badly formed; in theory we only want to ignore cases
            # where the leaf key is not present.
            pass
        except:
            raise

    @tidy_vimfaults
    def reg_values_list( self, path, match='^.*', expand=False, wow=None, detailed=False ):
        res = self.regmgr.ListRegistryValuesInGuest(
            vm            = self.vm,
            auth          = self.auth,
            keyName       = self._mkRegKeyNameSpec( path, wow ),
            expandStrings = expand,
            matchPattern  = match )
        if detailed:
            return { elt.name.name :
                     { 'path'  : elt.name.keyName.registryPath,
                       'wow'   : elt.name.keyName.wowBitness,
                       'type'  : type( elt.data ),
                       'value' : elt.data.value, }
                     for elt in res }
        else:
            return { elt.name.name : elt.data.value for elt in res }

    def reg_value_get( self, path, name, expand=False, wow=None, detailed=False ):
        try:
            return self.reg_values_list(
                path,
                match    = '^{}$'.format( re.escape( name )),
                expand   = expand,
                wow      = wow,
                detailed = detailed )[ name ]
        except KeyError:
            raise NameNotFoundError( name, 'value not found in ' + path )

    @tidy_vimfaults
    def reg_value_set( self, path, name, value, type=None, wow=None ):
        def guess_type():
            t = __builtin__.type( value )
            # n.b. I don't know how to infer REG_EXPAND_SZ
            if t is int:                return self.REG_DWORD
            if t is long:               return self.REG_QWORD
            if t is unicode:            return self.REG_SZ
            if t is list:               return self.REG_MULTI_SZ
            if t is bytearray:          return self.REG_BINARY
            # This is probably wrong for raw utf16
            if value.find( '\0' ) >= 0: return self.REG_BINARY
            raise TypeError( 'Cannot infer type for registry value' )

        if not type:
            try:
                prev = self.reg_value_get( path, name, wow=wow, detailed=True )
                type = prev[ 'type' ]
            except ( KeyError, NameNotFoundError ):
                type = guess_type()
        valspec = vim.vm.guest.WindowsRegistryManager.RegistryValue(
            name = self._mkRegValNameSpec( path, name, wow ),
            data = type( value = value ) )
        self.regmgr.SetRegistryValueInGuest(
            vm    = self.vm,
            auth  = self.auth,
            value = valspec )

    @tidy_vimfaults
    def reg_value_delete( self, path, name, wow=None ):
        try:
            self.regmgr.DeleteRegistryValueInGuest(
                vm        = self.vm,
                auth      = self.auth,
                valueName = self._mkRegValNameSpec( path, name, wow ) )
        except vim.fault.GuestRegistryValueNotFound:
            pass

# end class _vmomiVmGuestOperation_Registry


######
## Public entry point for guest ops
## Users should usually use vmomiConnect.vmguest_ops() since it requires an
## existing connection and virtual machine object.
######

class vmomiVmGuestOperation( _vmomiVmGuestOperation_Env,
                             _vmomiVmGuestOperation_Dir,
                             _vmomiVmGuestOperation_File,
                             _vmomiVmGuestOperation_Registry,
                             _with ):
    def __init__( self, vsi, vm, *args, **kwargs ):
        kwargs = dict( **kwargs ) # copy; destructively modified
        for arg in args:
            if isinstance( arg, argparse.Namespace ):
                kwargs.update( vars( arg ))

        content      = vsi.si.content
        gomgr        = content.guestOperationsManager
        self.almgr   = gomgr.aliasManager
        self.fmgr    = gomgr.fileManager
        self.pmgr    = gomgr.processManager
        self.regmgr  = gomgr.guestWindowsRegistryManager
        #self.sessid = content.sessionManager.currentSession.key

        self.vsi     = vsi
        self.vm      = vm

        self.environ = kwargs.get( 'environ' ) # optional

        if self.vm.config.guestId.find( 'win' ) == 0:
            self.ostype = WinNT
        else:
            self.ostype = POSIX

        self.auth = vim.vm.guest.NamePasswordAuthentication()
        for authparm in ('username', 'password'):
            try:
                val = kwargs[ authparm ]
            except KeyError:
                val = self.vsi.kwargs[ 'guest_' + authparm ]

            if callable( val ):
                setattr( self.auth, authparm, val( self, authparm ))
            else:
                setattr( self.auth, authparm, val )

        # Defaults to user's homedir on linux
        self.cwd = kwargs.get( 'cwd' ) or kwargs.get( 'workingDirectory' )
        self.tmpfile = []
        self.tmpdir  = []

    def __del__( self ):
        try:
            self._gc_tmpfiles( files=self.tmpfile, dirs=self.tmpdir )
        except:
            pass

    def _printdbg( self, *text ):
        def expand( txt ):
            result = []
            for elt in txt:
                if callable( elt ):
                    result.extend( expand( elt() ))
                elif isinstance( elt, vim.vm.guest.FileManager.FileAttributes ):
                    attr = self.decodeFileAttributes( elt )
                    p = []
                    for k in attr:
                        v = '0{:o}'.format( attr[ k ] ) if k == 'mode' \
                            else str( attr[ k ] )
                        p.append( str.join( '=', (k, v) ))
                    s = str.join( ', ', sorted( p ))
                    result.append( s )
                else:
                    result.append( str( elt ))
            if result:
                return result

        if debug and text:
            text = str.join( ' ', expand( text ))
            printerr( 'debug', self.vm.name, text )

    # Using self.vm.runtime.host.config.certificate directly would require
    # retrieving all of the properties in config first.  Using the property
    # collector retrieves just the value we want and is much faster.
    def host_cert( self ):
        vsi     = self.vsi
        host    = self.vm.runtime.host
        vimtype = type( host )
        prop    = 'config.certificate'
        res     = vsi.get_obj_props( [ vimtype ], [ prop ], root=[ host ] )[0]
        # convert cert from byte array to string
        return str.join( '', ( chr( c ) for c in res[ prop ] ))

    def run( self, *args, **kwargs):
        return vmomiVmGuestProcess( self, *args, **kwargs )

    @tidy_vimfaults
    def ps( self, *pids ):
        return self.pmgr.ListProcessesInGuest(
            vm   = self.vm,
            auth = self.auth,
            pids = list( pids ))

    @tidy_vimfaults
    def kill( self, pid ):
        self._printdbg( 'kill', pid )
        try:
            return self.pmgr.TerminateProcessInGuest(
                vm   = self.vm,
                auth = self.auth,
                pid  = pid )
        except vim.fault.GuestProcessNotFound:
            pass

# end class vmomiVmGuestOperation


######
## Run programs in guest.
## Usually generated by vmomiVmGuestOperation.run()
######

class vmomiVmGuestProcess( object ):
    result = property( lambda self: self.wait( once=True ) )

    def __init__( self, parent,
                  script = None,
                  output = True,
                  wait   = True,
                  cwd             = None,
                  environ         = None,
                  script_file     = None,
                  separate_stderr = False ):

        if script_file:
            script = file_contents( script_file )
        elif script is None:
            raise GuestOperationError( '''one of `script' or `script_file' arg is not optional''' )

        self.parent  = parent
        self.cwd     = cwd or parent.cwd
        self.environ = environ or parent.environ
        # os.environ is not an instance of type dict, but it acts like one.
        if hasattr( self.environ, '__getitem__' ):
            self.environ = dict_to_environ( self.environ )

        scriptperm = 0o700
        devnull    = '/dev/null'

        # Use .cmd for script suffix so that it will also execute on WinNT
        scriptfile   = parent.mktemp( suffix='.cmd' )
        self.prog    = scriptfile
        self.tmpfile = { 'script' : scriptfile }
        script += '\n'
        if parent.ostype is WinNT:
            script.replace( '\n', '\r\n' )
            scriptperm = None
            devnull    = ':NUL'
        elif script.find( '#!' ) != 0 and script.find( '\x7fELF' ) != 0:
            script = '#!/bin/sh\n' + script
        parent.put_file( scriptfile, script, perm=scriptperm, overwrite=True )

        if separate_stderr:
            self.tmpfile[ 'stdout' ] = scriptfile + '.out'
            self.tmpfile[ 'stderr' ] = scriptfile + '.err'
            self.args = '>{} 2>{}'.format(
                self.tmpfile[ 'stdout' ],
                self.tmpfile[ 'stderr' ] )
        elif output:
            self.tmpfile[ 'stdout' ] = scriptfile + '.out'
            self.args = '>{} 2>&1'.format( self.tmpfile[ 'stdout' ])
        else:
            self.args = '>{} 2>&1'.format( devnull )

        self._result = None
        self.start()
        if wait:
            self.wait()

    @tidy_vimfaults
    def start( self ):
        parent = self.parent
        pspec = vim.vm.guest.ProcessManager.ProgramSpec(
            workingDirectory = self.cwd,
            envVariables     = self.environ,
            programPath      = self.prog,
            arguments        = self.args )

        self.pid = parent.pmgr.StartProgramInGuest(
            vm   = parent.vm,
            auth = parent.auth,
            spec = pspec )
        parent._printdbg( 'exec', self.prog, ': pid', self.pid )

    def kill( self ):
        self.parent.kill( self.pid )

    def wait( self, once=False ):
        if self._result:
            return self._result

        parent = self.parent
        pid    = self.pid
        delay  = 1
        while True:
            res = parent.ps( pid )
            if not res:
                # pid not found, process info has been lost.
                # should we raise an exception here?
                return
            if res[0].exitCode is None:
                if once:
                    return
                time.sleep( delay )
                if delay < 10:  # increase delay up to 10s
                    delay += 1
                continue

            res = res[0]
            result = pseudoPropAttr()
            result.update( { 'startTime' : res.startTime,
                             'endTime'   : res.endTime,
                             'exit'      : res.exitCode })
            parent = self.parent
            parent._printdbg( 'pid', self.pid, 'exit code', res.exitCode )
            if self.tmpfile.get( 'stdout' ):
                result[ 'output' ] = parent.get_file( self.tmpfile[ 'stdout' ])
            if self.tmpfile.get( 'stderr' ):
                result[ 'stderr' ] = parent.get_file( self.tmpfile[ 'stderr' ])
            parent._gc_tmpfiles( files=self.tmpfile.values() )
            self._result = result
            return result

# end class vmomiVmGuestProcess


######
##
######

class vmomiTaskWait( object ):
    def __init__( self, parent, tasklist, printsucc=True, callback=None ):
        try:
            isiterable = iter( tasklist )
        except TypeError:
            tasklist = [ tasklist ]
        self.callback  = callback
        self.succ      = 1

        self.parent    = parent
        self.tasklist  = tasklist
        self.taskleft  = [ task.info.key for task in tasklist ]
        self.printsucc = printsucc

    def diag_callback( self, err, *args ):
        printerr( 'Callback error', err )
        print( args, file=sys.stderr )
        # Perhaps we can do something more useful here depending on the
        # type of error.  For now, just stop further callbacks
        self.callback = None

    def tw_callback( self, change, objSet, filterSet, update ):
        if self.callback:
            try:
                self.callback( change, objSet, filterSet, update )
            except Exception as err:
                self.diag_callback( err, change, objSet, filterSet, update )

        if change.name == 'info':
            state = change.val.state
        elif change.name == 'info.state':
            state = change.val
        else:
            return

        info = objSet.obj.info
        if state == vim.TaskInfo.State.success:
            self.taskleft.remove( info.key )
            if self.printsucc:
                print( info.entityName, 'Success', sep=': ' )
        elif state == vim.TaskInfo.State.error:
            self.taskleft.remove( info.key )
            self.succ = 0
            if not self.callback:
                printerr( info.entityName, info.error.msg )

        if not self.taskleft:
            return self.succ

    def wait( self ):
        return self.parent.monitor_property_changes( self.tasklist, [], self.tw_callback )



######
## vmomi utility routines
######

def get_seq_type( obj, typeref ):
    return filter( lambda elt: isinstance( elt , typeref ), obj )


def attr_get( obj, name ):
    for elt in obj:
        if getattr( elt, 'key' ) == name:
            return getattr( elt, 'value' )

def attr_to_dict( obj, objtype=dict ):
    return objtype( ( getattr( o, 'key' ),
                      getattr( o, 'value' ) )
                    for o in obj )


def propset_get( propset, name ):
    try:
        propset = propset.propSet
    except AttributeError:
        pass
    for elt in propset:
        if elt.name == name:
            return elt.val

def propset_to_dict( propset, objtype=dict ):
    try:
        propset = propset.propSet
    except AttributeError:
        pass
    return objtype( (p.name, p.val) for p in propset )

def flat_to_nested_dict( flat, sep='.', objtype=dict ):
    '''
    Convert a dict with keys of the form 'a.b.c', 'a.b.d', etc. into
    a hierarchical tree of nested dicts.

    The optional keyword arg SEP can be used to change the separator.

    If there are keys which are prefixes of other keys, the shorter key's
    value will be moved to the None slot of the subkey dict, since a key
    cannot have both an immediate end value and a link to subkeys.

    For example, given

            { 'a.b.c' : 1,
              'd.e.f' : 2, }

    the result will be

            { 'a' : { 'b' : { 'c' : 1 }, },
              'd' : { 'e' : { 'f' : 2 }, }, }

    but given

            { 'a.b'   : 0,
              'a.b.c' : 1,
              'd.e.f' : 2, }

    the result will be

            { 'a' : { 'b' : { None : 0,
                              'c'  : 1 }, },
              'd' : { 'e' : { 'f'  : 2 }, }, }


    The optional keyword arg OBJTYPE can be used to specify the datatype for
    the new tree object.  For example using objtype=pseudoPropAttr provides a
    dict object whose keys can also be accessed as objects, e.g.

            result['a']['b']['c'] == result.a.b.c
    '''
    nested = objtype()
    for k in flat:
        parts = k.split( sep )
        walk = nested
        for elt in parts[ 0 : -1 ]: # all but last
            try:
                if not isinstance( walk[ elt ], objtype ):
                    prev_elt = walk[ elt ]
                    walk[ elt ] = objtype()
                    walk[ elt ][ None ] = prev_elt
            except KeyError:
                walk[ elt ] = objtype()
            walk = walk[ elt ]
        walk[ parts[ -1 ] ] = flat[ k ]
    return nested

def environ_to_dict( names, preserve_case=False ):
    res = {}
    for elt in names:
        k, v = elt.split( '=', 1 )
        if preserve_case:
            res[ k ] = v
        else:
            res[ k.upper() ] = v
    return res

def dict_to_environ( names ):
    return sorted( str.join( '=', (k, names[ k ])) for k in names )

# n.b. this only works if values are hashable
def inverted_dict( d ):
    return { v : k for k, v in d.items() }


######
## generic utility routines
######

# This doesn't just use the textwrap class because we can do a few special
# things here, such as avoiding filling command examples
def fold_text( text, maxlen=75, indent=0 ):
    text = text.expandtabs( 8 )

    text      = re.sub( '\r', '', text )             # CRLF -> LF
    paragraph = re.split( '\n\n', text, flags=re.M ) # Split into separate chunks.

    re_ll = re.compile( '(.{1,%s})(?:\s+|$)' % maxlen, flags=re.M )
    filled = []
    for para in paragraph:
        if re.match( '^\s*[#$]', para ):
            filled.append( para )
            continue

        # Remove all newlines, replacing trailing/leading
        # whitespace with a single space.
        #para = re.sub( '\\s*\n\\s*', ' ', para, flags=re.M )
        # Only unfill if line is >= 42 chars
        para = re.sub( '(?<=\S{42})\\s*\n\\s*', ' ', para, flags=re.M )

        # split into lines no longer than maxlen but only at whitespace.
        para = re.sub( re_ll, '\\1\n', para )
        # but remove final newline
        para = re.sub( '\n+$', '', para, flags=re.M )
        filled.append( para )

    text = str.join( '\n\n', filled ) # rejoin paragraphs at the end.
    if indent:
        repl = '\n' + (' ' * indent)
        text = re.sub( '\n', repl, text, flags=re.M )

    return text


def str_to_bytes( str_val ):
    unit = { 'b'   : 512,

             'k'   : 1024,         't'   : 1024 ** 4,
             'kib' : 1024,         'tib' : 1024 ** 4,
             'kb'  : 1000,         'tb'  : 1000 ** 4,

             'm'   : 1024 ** 2,    'p'   : 1024 ** 5,
             'mib' : 1024 ** 2,    'pib' : 1024 ** 5,
             'mb'  : 1000 ** 2,    'pb'  : 1000 ** 5,

             'g'   : 1024 ** 3,    'e'   : 1024 ** 6,
             'gib' : 1024 ** 3,    'eib' : 1024 ** 6,
             'gb'  : 1000 ** 3,    'eb'  : 1000 ** 6, }
    regex = re.compile( "^\s*(\d+)\s*([bkmgtpei]+)\s*$", flags=re.I )
    match = regex.search( str( str_val ) )
    if match:
        size, factor = match.groups()
        return long( size ) * unit[ factor.lower() ]
    else:
        return long( str_val )


def scale_size( size, fmtsize=1024 ):
    # x & (x-1) == 0 iff x == 2^n
    # if x == 2^n, only nth bit in x is set.
    # subtracting 1 flips all bits via a borrow; the logical AND is zero.
    # If x != 2^n, x-1 will flip all bits up to and including the first 1, but
    # will not negate the entire value and an AND will not produce zero.
    def pow2p( n ):
        return (n & (n - 1) == 0)

    if size is None or size == 0:
        return '0 B'

    suffix = (' B', ' K', ' M', ' G', ' T', ' P', ' E')
    idx = 0

    try:
        ispow2 = pow2p( size )
        if not ispow2 or not pow2p( fmtsize ):
            size = float( size )
    except TypeError: # size is already float?
        ispow2 = False

    while size > fmtsize:
        size = size / fmtsize
        idx += 1

    if ispow2 and fmtsize == 1024:
        fmtstr = '%d%s%s'
        if size < 10: # Prefer 4096M to 4G
            size *= fmtsize
            idx -= 1
    elif size < 100 and idx > 0:
        fmtstr = '%.2f%s%s'
    else:
        size = round( size )
        fmtstr = '%d%s%s'

    if idx == 0:           unit = ''
    elif pow2p( fmtsize ): unit = 'iB'
    else:                  unit =  'B'

    return fmtstr % (size, suffix[ idx ], unit)

def printerr( *args, **kwargs ):
    sep  = kwargs.get( 'sep',  ': ' )
    end  = kwargs.get( 'end',  '\n' )
    file = kwargs.get( 'file', sys.stderr )

    pargs = args
    if kwargs.has_key( 'progname' ):
        if kwargs[ 'progname' ] is not None:
            pargs = list( args )
            pargs.insert( 0, kwargs[ 'progname' ] )
    elif progname:
        pargs = list( args )
        pargs.insert( 0, progname )
    print( *pargs, sep=sep, end=end, file=file )

def file_contents( filename, mode='r' ):
    with open( filename, mode ) as f:
        return f.read()


def y_or_n_p( prompt, yes='y', no='n', response=None, default=None ):
    if response is None:
        response = { 'y' : True,  'n' : False }
    c = ' ({} or {}) '
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
                     yes      = 'yes',
                     no       = 'no',
                     response = { 'yes' : True,
                                  'no'  : False },
                     default  = default )

# eof
