# $Id: vspherelib.py,v 1.2 2018/03/23 10:20:19 friedman Exp $

from __future__ import print_function

import argparse
import atexit
import getpass
import os
import sys
import ssl
import re

from pyVim   import connect as pyVconnect
from pyVmomi import vim, vmodl


def get_args_setup():
    env = os.environ

    parser = argparse.ArgumentParser()
    parser.add_argument( '-s', '--host',     required=True,           help='Remote esxi/vcenter host to connect to' )
    parser.add_argument( '-o', '--port',     default=443, type=int,   help='Port to connect on' )
    parser.add_argument( '-u', '--user',     default=env['LOGNAME'],  help='User name for host connection' )
    parser.add_argument( '-p', '--password',                          help='Server user password' )

    def getpass( args ):
        if args.password:
            return
        elif os.getenv( 'VMPASSWD' ):
            args.password = os.getenv( 'VMPASSWD' )
        else:
            prompt = 'Enter password for %(user)s@%(host)s: ' % vars( args )
            args.password = getpass.getpass( prompt )
    parser.checkpass = getpass

    return parser


def vmlist_sort_by_args( vmlist, args ):
    vmorder = dict()
    i = 0
    for name in args.vm:
        vmorder[name] = i
        i += 1
    cmpfn = lambda a,b: cmp( vmorder[a.name], vmorder[b.name] )
    if type(vmlist[0]) is vmodl.query.PropertyCollector.ObjectContent:
        cmpfn = lambda a,b: cmp( vmorder[a.obj.name], vmorder[b.obj.name] )
    vmlist.sort( cmp=cmpfn )


def hconnect( args ):
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

    atexit.register( pyVconnect.Disconnect, si )
    return si


def filter_spec( container, props ):
    if type(props) is dict:
        props = props.keys()
    elif type(props) is str:
        props = [props]

    vpc        = vmodl.query.PropertyCollector
    travSpec   = vpc.TraversalSpec( name='traverseEntities',
                                    path='view',
                                    skip=False,
                                    type=type( container ) )
    objSpec    = [vpc.ObjectSpec( obj=container, skip=True, selectSet=[travSpec] )]
    propSpec   = vpc.PropertySpec( type=type( container.view[0] ),
                                   pathSet=props,
                                   all=not props and not len( props ))
    filterSpec = vpc.FilterSpec( objectSet=objSpec, propSet=[propSpec] )
    return filterSpec


def get_obj_props( si, vimtype, props=None, root=None, recur=True ):
    if root is None:
        root = si.content.rootFolder
    cvM = si.content.viewManager
    container = cvM.CreateContainerView( container=root, type=vimtype, recursive=recur )
    result = None
    if props is None:
        result = container.view
    else:
        spc = si.content.propertyCollector
        filterSpec = filter_spec( container, props )
        res = spc.RetrieveContents( [filterSpec] )
        if res:
            if type( props ) is dict:
                match = []
                for r in res:
                    for prop in r.propSet:
                        want = props.get( prop.name )
                        if want and prop.val in want:
                            match.append( r )
                if len( match ) > 0:
                    result = match
            else:
                result = res
    container.Destroy()
    return result


def get_obj( *args, **kwargs):
    result = get_obj_props( *args, **kwargs )
    if result:
        if kwargs.get( 'props' ) or len(args) > 2:
            return [elt.obj for elt in result]
        else:
            return result


def get_attr( obj, name ):
    for elt in obj:
        if getattr( elt, 'key' ) == name:
            return getattr( elt, 'value' )


def get_attr_dict( obj ):
    attrs = dict()
    for elt in obj:
        key = getattr( elt, 'key' )
        val = getattr( elt, 'value' )
        attrs[key] = val
    return attrs


def get_propset( propset, name ):
    if type( propset ) is vmodl.query.PropertyCollector.ObjectContent:
        propset = propset.propSet
    search = filter( lambda elt: elt.name == name, propset )
    if search and len( search ) > 0:
        return search[0].val


def get_seq_type( obj, typeref ):
    return filter( lambda elt: issubclass( type( elt ), typeref ), obj)


def taskwait( si, tasklist, printsucc=True ):
    spc = si.content.propertyCollector
    vpc = vmodl.query.PropertyCollector

    objSpecs   = [vpc.ObjectSpec( obj=task ) for task in tasklist]
    propSpec   =  vpc.PropertySpec( type=vim.Task, pathSet=[], all=True )
    filterSpec =  vpc.FilterSpec( objectSet=objSpecs, propSet=[propSpec] )
    filter     =  spc.CreateFilter( filterSpec, True )

    succ     = 1
    taskleft = [task.info.key for task in tasklist]
    try:
        version, state = None, None

        while len( taskleft ):
            update = spc.WaitForUpdates( version )

            for filterSet in update.filterSet:
                for objSet in filterSet.objectSet:
                    info = objSet.obj.info

                    for change in objSet.changeSet:
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
                            printerr( info.entityName, info.error.msg )
            version = update.version
    finally:
        if filter:
            filter.Destroy()
    return succ


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
        fmtstr = "%d %s%s"

    if pow2p( fmtsize ): unit = "iB"
    else:                unit =  "B"

    return fmtstr % (size, suffix[idx], unit)


def printerr( *args, **kwargs ):
    sep  = kwargs.get( 'sep',  ': ' )
    end  = kwargs.get( 'end',  "\n" )
    file = kwargs.get( 'file', sys.stderr )
    print( *args, sep=sep, end=end, file=sys.stderr )


# eof
