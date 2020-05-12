#!/usr/bin/env python

# 2020-05-11
# Experimenting with parsing ovf xml in order to edit attributes before
# uploading to the vsphere parser, because the parser may reject some
# specifications outright, leaving us with no other parsed data structure.
#
# This or something like it is intended for vsphere-vm-deploy.


from   __future__ import print_function

import io
import re
import xml.etree.ElementTree as ET

import pprint
repl_pp = pprint.PrettyPrinter( indent=2, width=40 )
def pp( *args, **kwargs ):
    for arg in args:
        repl_pp.pprint( arg, **kwargs )


class MET (ET.ElementTree):
    class Container( object ): pass

    super = property( lambda self: super( type( self ), self ) )

    def __init__( self, *args, **kwargs ):
        self.MET = self.Container()
        self.MET.filename = None
        self.MET.ns_map   = {}
        self.MET.xml      = None
        self.MET.xmlns    = None

        try:
            self.MET.filename = kwargs[ 'filename' ]
            del kwargs[ 'filename' ]
        except KeyError:
            pass

        self.super.__init__( *args, **kwargs )
        if self.MET.filename:
            self.parse( self.MET.filename )


    def parse( self, filename ):
        events = ("start-ns", "start")
        root   = None
        ns_map = self.MET.ns_map

        with open( filename ) as fh:
            text = unicode( fh.read() )
        # remove default namespace
        # n.b. need to put it back before convering back to xml
        text = re.sub( '\\sxmlns=".*?"', '', text, count=1 )
        text = io.StringIO( text )

        for event, elem in ET.iterparse( text, events ):
            if event == "start-ns":
                ns_map.update( (elem,) )
            elif event == "start" and root is None:
                root = elem

        self.MET.xml = ET.ElementTree( root )


    def find( self, *args ):
        if len( args ) < 2:
            args = list( args ) # copy
            args.append( self.MET.ns_map )
        pp(args)
        return self.MET.xml.find( *args )


xml = MET( filename='osx1015-template.ovf' )
node = xml.find( './VirtualSystem/Name' )
pp( node.text )

# eof
