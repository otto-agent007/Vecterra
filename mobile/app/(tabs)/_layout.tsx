import { Tabs } from 'expo-router';
import { Text, type ColorValue } from 'react-native';

function TabIcon({ glyph, color }: { glyph: string; color: ColorValue }) {
  return <Text style={{ fontSize: 18, color }}>{glyph}</Text>;
}

export default function TabsLayout() {
  return (
    <Tabs screenOptions={{ tabBarActiveTintColor: '#0b7285' }}>
      <Tabs.Screen
        name="index"
        options={{ title: 'Map', tabBarIcon: ({ color }) => <TabIcon glyph="◎" color={color} /> }}
      />
      <Tabs.Screen
        name="queue"
        options={{ title: 'Upload queue', tabBarIcon: ({ color }) => <TabIcon glyph="⇪" color={color} /> }}
      />
    </Tabs>
  );
}
