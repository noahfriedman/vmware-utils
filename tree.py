#!/usr/bin/python

from __future__ import print_function
from datetime   import datetime

import code
import pprint
import re
import sys

class Container( object ): pass
vsl = Container()


repl_pp = pprint.PrettyPrinter( indent=2, width=40 )
def pp( *args, **kwargs ):
    for arg in args:
        repl_pp.pprint( arg, **kwargs )


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

vsl.fold_text = fold_text


stdindent  =  -17
indentL    = stdindent - 3
indentS    = " " * abs( indentL )
farindentL = stdindent - 10
farindentS = " " * abs( farindentL )

def p( *parm ):
    if parm[ 1 ] is None:
        return
    w = stdindent
    if isinstance( parm[ 0 ], (int, long) ):
        w = parm[ 0 ]
        parm = parm[ 1: ]
    end = '\n'
    if isinstance( parm[ -1 ], (str, unicode) ) and parm[ -1 ] == "":
        end  = ''
        parm = parm[ : -1 ]

    if len( parm ) > 1 and isinstance( parm[ 1 ], list):
        val = str.join( '\n' + farindentS + indentS + '   ', map( str, parm[ 1 ] ))
    else:
        val = str.join( ' ', map( str, parm[ 1: ] ) )
    s = '%*s : %s' % (w, parm[ 0 ], val )
    print( s, end=end )

def pi( *parm ):
    print( indentS, end="" )
    p( *parm )

def pf( label, dic, attrs, extra='' ):
    if label:
        p( label, extra )
    for n, attr in enumerate( attrs ):
        try:
            if n == 0 and label and not extra:
                fn = p
            else:
                fn = pi

            if type( attr ) is tuple:
                fn( farindentL, attr[0], dic[ attr[1] ] )
            else:
                fn( farindentL, attr, dic[ attr ] )

        except KeyError:
            pass


class OrderedStrings( list ):
    class _str( str ):
        def __init__( self, s ):
            self.value = s
            self.order = []
        def cmp( self, other ):
            try:
                return cmp( self.order[ self.value ], self.order[ other ] )
            except KeyError:
                return cmp( str( self.value ), str( other ) )
        def __eq__( self, arg ): return self.cmp( arg ) == 0
        def __lt__( self, arg ): return self.cmp( arg ) <  0
        def __le__( self, arg ): return self.cmp( arg ) <= 0
        def __gt__( self, arg ): return self.cmp( arg ) >  0
        def __ge__( self, arg ): return self.cmp( arg ) >= 0
        def __ne__( self, arg ): return self.cmp( arg ) != 0
        def __str__( self ):     return self.value.__str__()
    def __init__( self, *values ):
        order = { s:i for i,s in enumerate( values ) }
        values = [ self._str( s ) for s in values ]
        # Cannot pass additional args to str instantiators
        for s in values: s.order = order
        self.extend( values )

class SnapshotTree( dict ):
    attrs = OrderedStrings( 'name', 'description', 'id', 'createTime', 'snapshot', 'childSnapshotList' )
    def __init__( self, name, date=None, id=0, description='', children=[] ):
        self.name              = name
        self.description       = description
        self.childSnapshotList = children
        self.id                = id
        self.snapshot          = None
        if date:
            self.createTime = datetime.strptime( date, "%Y-%m-%d %H:%M:%SZ" )
        else:
            self.createTime = datetime.now()
    def _to_dict( self ):
        d = { x : getattr( self, x ) for x in reversed(self.attrs) }
        d[ 'childSnapshotList' ] = [ x._to_dict() for x in self.childSnapshotList ]
        return d
    def __len__(     self ):           return len( self.attrs )
    def __str__(     self ):           return repl_pp.pformat( self )
    def __iter__(    self ):           return self.attrs.__iter__()
    def __getitem__( self, key ):      return getattr( self, key )
    def __setitem__( self, key, val ): return setattr( self, key, val )
    def __delitem__( self, key ):      return delattr( self, key )
    def __contains__( self, key ):     return key in self.attrs
    def keys(   self ): return list( self.attrs )
    def values( self ): return [ self[ x ] for x in self.keys() ]
    def items(  self ): return [ (x, getattr( self, x )) for x in self.attrs ]
    def viewkeys(   self ): return self._to_dict().viewkeys()
    def viewvalues( self ): return self._to_dict().viewvalues()
    def viewitems(  self ): return self._to_dict().viewitems()

class SnapshotInfo( object ):
    def __init__( self, current=None, tree=None ):
        self.currentSnapshot  = current
        self.rootSnapshotList = [ tree ]


class SampleData( object ):
    def __init__( self ):
        current = SnapshotTree( "Before 2019.2 CLean",             "2019-09-25 21:17:40Z", 14, )
        tree    = SnapshotTree(
            "Drive Enlarged",                                      "2019-09-20 03:02:12Z",  1,
            "Used directions to update the size of the install drive to 30 GB.",
            [ SnapshotTree(
                "Desktop Installed",                               "2019-09-20 03:20:15Z",  2,
                "Now have basic desktop no Greenhills related installations in place.",
                [ SnapshotTree(
                    "Greenhills Basic Dev",                        "2019-09-20 03:50:57Z",  3,
                    "Basic tools and ability to get repository installed.\n"
                    "No source code at this point.",
                    [ SnapshotTree(
                        "Just Before Build",                       "2019-09-20 04:08:52Z",  4,
                        "Tools all install, git and python added, etc.\n"
                        "PRQA Tools have been downloaded, but not installed yet.",
                        [ SnapshotTree(
                            "flob",                                "2019-12-01 00:00:00Z", 15,
                            "test"
                            "test 2",
                            [ SnapshotTree( "Failure2",            "2019-12-01 12:15:35Z", 16, ),
                            ] )
                        ] ),
                      SnapshotTree(
                        "Just Before Build",                       "2019-09-20 04:08:52Z",  4,
                        "Tools all install, git and python added, etc.\n"
                        "PRQA Tools have been downloaded, but not installed yet.",
                        [ SnapshotTree(
                            "Most Parsing",                        "2019-09-24 19:06:34Z", 11,
                            "This is a point where the basics are parsing and set up with just the parser, but no compliance.\n"
                            "There is a test area for .i files.",
                            [ SnapshotTree( "Failure1",            "2019-09-25 12:15:35Z", 13, ),
                              current,
                            ] )
                        ] )
                    ] )
                ] )
            ] )
        self.snapshot = SnapshotInfo( current, tree )


class Display( object ):
    def __init__( self, vm, verbose=2 ):
        self.vm      = vm
        self.verbose = verbose

    def display_snapshot_tree( self ):
        vm_snapshot = self.vm.snapshot

        def walktree( fn, snap_list=vm_snapshot.rootSnapshotList, depth=0 ):
            result = []
            for snap in snap_list:
                result.append( fn( snap, depth ))
                result.extend( walktree( fn, snap.childSnapshotList, depth+1 ))
            return result

        snapshot_list = walktree( lambda x, _: x )

        def maxlen( attr ):
            return max( len( str( getattr( x, attr ))) for x in snapshot_list )

        verbose     = self.verbose
        indent_incr = 4
        max_depth   = 1 + max( walktree( lambda _, depth: depth ))
        max_namelen = maxlen( 'name' ) + max_depth * indent_incr
        max_desc_col = 78 + indentL  # n.b. indentL is negative

        def snap_timestamp( snap ):
            ct = snap.createTime.replace( microsecond=0, tzinfo=None )
            fmt = '%Y-%m-%d %H:%M:%SZ' if verbose else '%y-%m-%d %H:%M'
            return ct.strftime( fmt )

        def format_snapshot( snap, depth=0 ):
            indent = depth * indent_incr
            num    = '#{}'.format( snap.id )
            name   = '"{}"'.format( snap.name )
            ts     = snap_timestamp( snap )
            fmt    = '* {2:{1:d}} {3} {4:>4}'

            w      = max( 2, max_namelen - indent )
            head   = fmt.format( num, w, name, ts, num )

            res = [ head ]
            if verbose:
                indent += 2
                desc = '  ' + vsl.fold_text( snap.description, indent=2, maxlen=max_desc_col-indent )
                if desc:
                    #desc = '| ' + desc.replace( '\n', '\n| ' )
                    res.extend( desc.split( '\n' ) )
                res.append( '' )
            return res

        def format_snaplist( snaplist=vm_snapshot.rootSnapshotList, depth=0 ):
            formatted = []
            leadws = ' ' * indent_incr

            for i, snap in enumerate( snaplist ):
                head  = format_snapshot( snap,                   depth=depth   )
                child = format_snaplist( snap.childSnapshotList, depth=depth+1 )
                if i + 1 < len( snaplist ):
                    for j in range( 1, len( head )):
                        head[ j ] = '| ' + head[ j ]
                    child = [ '| ' + line for line in child ]

#                if verbose and len( head ) > 2:
#                    for j, line in enumerate( head ):
#                        if j < 1:
#                            head[ j ] = '+---' + head[ j ]
#                        else:
#                            head[ j ] = leadws + '|  ' + head[ j ]
                formatted.extend( head )
                formatted.extend( child )

            return [ leadws + x for x in formatted ]


        #tree = '\n'.join( format_snaplist() ).replace( '\n', '\n' + indentS ).rstrip()
        tree = '\n' + '\n'.join( format_snaplist() )
        p( 'Snapshot tree', tree )

        #cur = [ x for x in snapshot_list
        #        if x.snapshot == vm_snapshot.currentSnapshot ][ 0 ]
        #p( 'Current snapshot', '#{} "{}"'.format( cur.id, cur.name ))


if __name__ == '__main__':
    vm     = SampleData()
    disp   = Display( vm ).display_snapshot_tree
    root   = vm.snapshot.rootSnapshotList[0]

    sys.displayhook = lambda arg: (arg is None) or pp( arg )
    code.interact( banner = '', local = locals() )
